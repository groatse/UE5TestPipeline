#include "PerfLifecycle.h"

#include "PerfFrameRecorder.h"
#include "Dom/JsonObject.h"
#include "Engine/Engine.h"
#include "Engine/GameInstance.h"
#include "Engine/LocalPlayer.h"
#include "GameFramework/Actor.h"
#include "GameFramework/GameModeBase.h"
#include "GameFramework/GameStateBase.h"
#include "GameFramework/Pawn.h"
#include "GameFramework/PlayerController.h"
#include "Misc/FileHelper.h"
#include "Misc/OutputDevice.h"
#include "ProfilingDebugging/MiscTrace.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/UnrealType.h"

namespace
{
	constexpr double PollIntervalSeconds = 0.1;

	bool ClassChainHasName(const UClass* Class, const FString& Name)
	{
		for (; Class; Class = Class->GetSuperClass())
		{
			if (Class->GetName() == Name)
			{
				return true;
			}
		}
		return false;
	}
}

/** Collects log lines that match a game event; matches fire on the game thread. */
class FPerfLogWatcher : public FOutputDevice
{
public:
	explicit FPerfLogWatcher(TArray<FString> InTexts) : Texts(MoveTemp(InTexts))
	{
		GLog->AddOutputDevice(this);
	}

	virtual ~FPerfLogWatcher() override
	{
		if (GLog)
		{
			GLog->RemoveOutputDevice(this);
		}
	}

	virtual bool CanBeUsedOnAnyThread() const override { return true; }

	virtual void Serialize(const TCHAR* Line, ELogVerbosity::Type Verbosity, const FName& Category) override
	{
		for (const FString& Text : Texts)
		{
			if (FCString::Strstr(Line, *Text))
			{
				FScopeLock Lock(&Mutex);
				Matched.AddUnique(Text);
			}
		}
	}

	TArray<FString> TakeMatches()
	{
		FScopeLock Lock(&Mutex);
		return MoveTemp(Matched);
	}

private:
	TArray<FString> Texts;
	TArray<FString> Matched;
	FCriticalSection Mutex;
};

FPerfLifecycle::FPerfLifecycle(FPerfFrameRecorder& InRecorder, const FString& EventsFile, const FString& InQuitOn)
	: Recorder(InRecorder)
	, QuitOn(InQuitOn)
{
	if (!EventsFile.IsEmpty())
	{
		LoadEvents(EventsFile);
	}

	TArray<FString> LogTexts;
	for (const FGameEvent& Event : Events)
	{
		if (!Event.LogText.IsEmpty())
		{
			LogTexts.Add(Event.LogText);
		}
	}
	if (LogTexts.Num() > 0)
	{
		LogWatcher = MakeUnique<FPerfLogWatcher>(MoveTemp(LogTexts));
	}

	PreLoadHandle = FCoreUObjectDelegates::PreLoadMap.AddRaw(this, &FPerfLifecycle::OnPreLoadMap);
	PostLoadHandle = FCoreUObjectDelegates::PostLoadMapWithWorld.AddRaw(this, &FPerfLifecycle::OnPostLoadMap);
	LoginHandle = FGameModeEvents::OnGameModePostLoginEvent().AddRaw(this, &FPerfLifecycle::OnPostLogin);
	MatchStateHandle = FGameModeEvents::OnGameModeMatchStateSetEvent().AddRaw(this, &FPerfLifecycle::OnMatchStateSet);
	TravelHandle = FWorldDelegates::OnSeamlessTravelStart.AddRaw(this, &FPerfLifecycle::OnSeamlessTravel);
	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FPerfLifecycle::Tick));

	Fire(TEXT("run_start"));
}

FPerfLifecycle::~FPerfLifecycle()
{
	Fire(TEXT("run_end"));
	FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	FCoreUObjectDelegates::PreLoadMap.Remove(PreLoadHandle);
	FCoreUObjectDelegates::PostLoadMapWithWorld.Remove(PostLoadHandle);
	FGameModeEvents::OnGameModePostLoginEvent().Remove(LoginHandle);
	FGameModeEvents::OnGameModeMatchStateSetEvent().Remove(MatchStateHandle);
	FWorldDelegates::OnSeamlessTravelStart.Remove(TravelHandle);
	if (UWorld* World = HookedWorld.Get())
	{
		World->RemoveOnActorSpawnedHandler(SpawnHandle);
	}
	for (const TPair<TWeakObjectPtr<APlayerController>, FDelegateHandle>& Hook : PawnHooks)
	{
		if (APlayerController* Controller = Hook.Key.Get())
		{
			Controller->GetOnNewPawnNotifier().Remove(Hook.Value);
		}
	}
}

void FPerfLifecycle::LoadEvents(const FString& EventsFile)
{
	FString Text;
	if (!FFileHelper::LoadFileToString(Text, *EventsFile))
	{
		UE_LOG(LogTemp, Error, TEXT("PerfPipeline: could not read events file %s"), *EventsFile);
		return;
	}
	TArray<TSharedPtr<FJsonValue>> Entries;
	const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Text);
	if (!FJsonSerializer::Deserialize(Reader, Entries))
	{
		UE_LOG(LogTemp, Error, TEXT("PerfPipeline: events file %s is not a JSON array"), *EventsFile);
		return;
	}

	for (const TSharedPtr<FJsonValue>& Value : Entries)
	{
		const TSharedPtr<FJsonObject>* Object = nullptr;
		if (!Value.IsValid() || !Value->TryGetObject(Object))
		{
			continue;
		}
		FGameEvent Event;
		(*Object)->TryGetStringField(TEXT("name"), Event.Name);
		(*Object)->TryGetBoolField(TEXT("repeat"), Event.bRepeat);
		FString Poll;
		if ((*Object)->TryGetStringField(TEXT("poll"), Poll))
		{
			Poll.Split(TEXT("."), &Event.PollObject, &Event.PollMember);
		}
		(*Object)->TryGetStringField(TEXT("spawn"), Event.SpawnClass);
		(*Object)->TryGetStringField(TEXT("log"), Event.LogText);

		const bool bRuntime = !Event.PollMember.IsEmpty() || !Event.SpawnClass.IsEmpty() || !Event.LogText.IsEmpty();
		if (!Event.Name.IsEmpty() && bRuntime)
		{
			Events.Add(MoveTemp(Event));
		}
	}
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: %d game events from %s"), Events.Num(), *EventsFile);
}

void FPerfLifecycle::Fire(const FString& Name, const FString& Detail)
{
	Recorder.AddMarker(Name, Detail);
	TRACE_BOOKMARK(TEXT("PerfMarker %s %s"), *Name, *Detail);
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: marker %s %s"), *Name, *Detail);

	if (!QuitOn.IsEmpty() && Name == QuitOn && !bQuitRequested)
	{
		bQuitRequested = true;
	}
}

bool FPerfLifecycle::Tick(float DeltaTime)
{
	if (LogWatcher.IsValid())
	{
		for (const FString& Text : LogWatcher->TakeMatches())
		{
			for (FGameEvent& Event : Events)
			{
				if (Event.LogText == Text && (Event.bRepeat || !Event.bFired))
				{
					Event.bFired = true;
					Fire(Event.Name, Text);
				}
			}
		}
	}

	const double Now = FPlatformTime::Seconds();
	if (Now - LastPollSeconds >= PollIntervalSeconds)
	{
		LastPollSeconds = Now;
		PollEvents();
	}
	return true;
}

UWorld* FPerfLifecycle::GameWorld() const
{
	if (!GEngine)
	{
		return nullptr;
	}
	for (const FWorldContext& Context : GEngine->GetWorldContexts())
	{
		if (Context.WorldType == EWorldType::Game && Context.World())
		{
			return Context.World();
		}
	}
	return nullptr;
}

UObject* FPerfLifecycle::PollTarget(const FString& Which) const
{
	UWorld* World = GameWorld();
	if (!World)
	{
		return nullptr;
	}
	if (Which == TEXT("GameMode"))
	{
		return World->GetAuthGameMode();
	}
	if (Which == TEXT("GameState"))
	{
		return World->GetGameState();
	}
	APlayerController* Controller = World->GetFirstPlayerController();
	if (Which == TEXT("PlayerController"))
	{
		return Controller;
	}
	if (Which == TEXT("Pawn"))
	{
		return Controller ? Controller->GetPawn() : nullptr;
	}
	return nullptr;
}

bool FPerfLifecycle::ReadBool(UObject* Object, const FString& Member) const
{
	UClass* Class = Object->GetClass();
	if (const FBoolProperty* Property = FindFProperty<FBoolProperty>(Class, *Member))
	{
		return Property->GetPropertyValue_InContainer(Object);
	}
	UFunction* Function = Class->FindFunctionByName(*Member);
	if (!Function || Function->NumParms != 1)
	{
		return false;
	}
	const FBoolProperty* Return = CastField<FBoolProperty>(Function->GetReturnProperty());
	if (!Return)
	{
		return false;
	}
	uint8* Params = static_cast<uint8*>(FMemory_Alloca(Function->ParmsSize));
	FMemory::Memzero(Params, Function->ParmsSize);
	Object->ProcessEvent(Function, Params);
	return Return->GetPropertyValue_InContainer(Params);
}

void FPerfLifecycle::PollEvents()
{
	for (FGameEvent& Event : Events)
	{
		if (Event.PollMember.IsEmpty() || (Event.bFired && !Event.bRepeat))
		{
			continue;
		}
		UObject* Target = PollTarget(Event.PollObject);
		const bool bValue = Target && ReadBool(Target, Event.PollMember);
		if (bValue && !Event.bLastPoll)
		{
			Event.bFired = true;
			Fire(Event.Name, FString::Printf(TEXT("%s.%s"), *Event.PollObject, *Event.PollMember));
		}
		Event.bLastPoll = bValue;
	}
}

void FPerfLifecycle::OnPreLoadMap(const FString& MapName)
{
	Fire(TEXT("map_load_start"), MapName);
}

void FPerfLifecycle::OnPostLoadMap(UWorld* World)
{
	Fire(TEXT("map_load_end"), World ? World->GetMapName() : FString());
	HookWorld(World);
}

void FPerfLifecycle::HookWorld(UWorld* World)
{
	if (!World || HookedWorld.Get() == World)
	{
		return;
	}
	if (UWorld* Old = HookedWorld.Get())
	{
		Old->RemoveOnActorSpawnedHandler(SpawnHandle);
	}
	HookedWorld = World;

	bool bWantsSpawns = false;
	for (const FGameEvent& Event : Events)
	{
		bWantsSpawns |= !Event.SpawnClass.IsEmpty();
	}
	if (bWantsSpawns)
	{
		SpawnHandle = World->AddOnActorSpawnedHandler(FOnActorSpawned::FDelegate::CreateRaw(this, &FPerfLifecycle::OnActorSpawned));
	}
}

void FPerfLifecycle::OnPostLogin(AGameModeBase* GameMode, APlayerController* Player)
{
	Fire(TEXT("player_login"), Player ? Player->GetName() : FString());
	if (Player)
	{
		const FDelegateHandle Handle = Player->GetOnNewPawnNotifier().AddLambda([this](APawn* Pawn)
		{
			if (Pawn)
			{
				Fire(TEXT("pawn_possessed"), Pawn->GetClass()->GetName());
			}
		});
		PawnHooks.Emplace(Player, Handle);
	}
}

void FPerfLifecycle::OnMatchStateSet(FName MatchState)
{
	Fire(TEXT("match_state"), MatchState.ToString());
}

void FPerfLifecycle::OnSeamlessTravel(UWorld* World, const FString& MapName)
{
	Fire(TEXT("travel"), MapName);
}

void FPerfLifecycle::OnActorSpawned(AActor* Actor)
{
	if (!Actor)
	{
		return;
	}
	for (FGameEvent& Event : Events)
	{
		if (!Event.SpawnClass.IsEmpty() && (Event.bRepeat || !Event.bFired) && ClassChainHasName(Actor->GetClass(), Event.SpawnClass))
		{
			Event.bFired = true;
			Fire(Event.Name, Actor->GetName());
		}
	}
}
