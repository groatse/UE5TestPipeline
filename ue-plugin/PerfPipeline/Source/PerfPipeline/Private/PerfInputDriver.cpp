#include "PerfInputDriver.h"

#include "PerfFrameRecorder.h"
#include "Async/Async.h"
#include "Components/InputComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "Engine/LocalPlayer.h"
#include "EnhancedInputComponent.h"
#include "EnhancedInputSubsystems.h"
#include "EnhancedPlayerInput.h"
#include "Framework/Application/SlateApplication.h"
#include "GameFramework/InputSettings.h"
#include "GameFramework/Pawn.h"
#include "GameFramework/PlayerController.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/FileManager.h"
#include "InputAction.h"
#include "InputMappingContext.h"
#include "InputKeyEventArgs.h"
#include "Misc/App.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/UObjectIterator.h"
#include "Widgets/SWindow.h"

namespace
{
	constexpr double DiscoverIntervalSeconds = 1.0;
	constexpr double CommandIntervalSeconds = 0.25;
	constexpr double StatusIntervalSeconds = 1.0;

	FString VectorText(const FVector& V, int32 Dimensions)
	{
		switch (Dimensions)
		{
		case 0:
		case 1: return FString::Printf(TEXT("%.2f"), V.X);
		case 2: return FString::Printf(TEXT("%.2f;%.2f"), V.X, V.Y);
		default: return FString::Printf(TEXT("%.2f;%.2f;%.2f"), V.X, V.Y, V.Z);
		}
	}

	const TCHAR* GestureName(uint8 Gesture)
	{
		static const TCHAR* Names[] = {TEXT("tap"), TEXT("double_tap"), TEXT("hold"), TEXT("direction"), TEXT("sweep")};
		return Names[Gesture];
	}

	bool IsMouseAxis(const FKey& Key)
	{
		return Key == EKeys::Mouse2D || Key == EKeys::MouseX || Key == EKeys::MouseY || Key == EKeys::MouseWheelAxis;
	}

	FString Quoted(const FString& Text)
	{
		return FString::Printf(TEXT("\"%s\""), *Text.Replace(TEXT("\""), TEXT("'")).Replace(TEXT("\n"), TEXT(" ")));
	}
}

FPerfInputDriver::FPerfInputDriver(FPerfFrameRecorder& InRecorder, const FString& ConfigFile, const FString& InOutputFolder)
	: Recorder(InRecorder)
	, OutputFolder(InOutputFolder)
{
	LoadConfig(ConfigFile);
	if (Seed == 0)
	{
		Seed = static_cast<int32>(FPlatformTime::Cycles() & 0x7fffffff) | 1;
	}
	Random.Initialize(Seed);
	Voices.SetNum(FMath::Max(1, VoiceCount));
	bStartMarkerSeen = StartOn.IsEmpty();
	Rows.Reserve(8192);
	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FPerfInputDriver::Tick));
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: input driver on, seed %d, start on '%s'"), Seed, *StartOn);
}

FPerfInputDriver::~FPerfInputDriver()
{
	FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	Write();
}

void FPerfInputDriver::LoadConfig(const FString& ConfigFile)
{
	FString Text;
	TSharedPtr<FJsonObject> Root;
	if (!FFileHelper::LoadFileToString(Text, *ConfigFile)
		|| !FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(Text), Root) || !Root.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("PerfPipeline: could not read play config %s; playing with defaults"), *ConfigFile);
		return;
	}
	Root->TryGetStringField(TEXT("start_on"), StartOn);
	Root->TryGetNumberField(TEXT("start_delay_s"), StartDelaySeconds);
	Root->TryGetNumberField(TEXT("voices"), VoiceCount);
	Root->TryGetNumberField(TEXT("mouse_scale"), MouseScale);
	Root->TryGetNumberField(TEXT("seed"), Seed);
	Root->TryGetNumberField(TEXT("play_s"), PlaySeconds);
	Root->TryGetStringField(TEXT("commands"), CommandFile);
	Root->TryGetStringArrayField(TEXT("exclude"), Exclude);
	Root->TryGetStringArrayField(TEXT("exclude_keys"), ExcludeKeys);
	const TSharedPtr<FJsonObject>* WeightObject = nullptr;
	if (Root->TryGetObjectField(TEXT("weights"), WeightObject))
	{
		for (const TPair<FString, TSharedPtr<FJsonValue>>& Pair : (*WeightObject)->Values)
		{
			Weights.Add(Pair.Key, static_cast<float>(Pair.Value->AsNumber()));
		}
	}
	if (!CommandFile.IsEmpty())
	{
		CommandFile = FPaths::ConvertRelativePathToFull(CommandFile);
	}
}

void FPerfInputDriver::OnMarker(const FString& Name)
{
	if (!bStartMarkerSeen && Name == StartOn)
	{
		bStartMarkerSeen = true;
	}
}

UWorld* FPerfInputDriver::GameWorld() const
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

bool FPerfInputDriver::IsExcluded(const FPlayAction& Action) const
{
	for (const FString& Pattern : Exclude)
	{
		if (Action.Name.MatchesWildcard(Pattern))
		{
			return true;
		}
	}
	for (const FKey& Key : Action.Keys)
	{
		if (ExcludeKeys.Contains(Key.ToString()))
		{
			return true;
		}
	}
	return false;
}

int32 FPerfInputDriver::FindOrAddAction(const FString& Name)
{
	for (int32 i = 0; i < Actions.Num(); ++i)
	{
		if (Actions[i].Name == Name)
		{
			return i;
		}
	}
	FPlayAction& Action = Actions.AddDefaulted_GetRef();
	Action.Name = Name;
	if (const float* Weight = Weights.Find(Name))
	{
		Action.Weight = FMath::Max(0.f, *Weight);
	}
	return Actions.Num() - 1;
}

void FPerfInputDriver::Discover(APlayerController* Controller)
{
	for (FPlayAction& Action : Actions)
	{
		Action.bActive = false;
	}

	UEnhancedPlayerInput* PlayerInput = Cast<UEnhancedPlayerInput>(Controller->PlayerInput);
	bLegacy = PlayerInput == nullptr;
	if (!bLegacy)
	{
		auto AddAction = [this](const UInputAction* InputAction, const FKey* Key, const TCHAR* Source)
		{
			if (!InputAction)
			{
				return;
			}
			FPlayAction& Action = Actions[FindOrAddAction(InputAction->GetName())];
			if (!Action.bActive)
			{
				// Fresh each discovery, so keys from contexts that were removed drop out.
				Action.Keys.Reset();
				Action.bMouse = false;
				Action.Source = Source;
			}
			Action.bActive = true;
			Action.Action = InputAction;
			switch (InputAction->ValueType)
			{
			case EInputActionValueType::Boolean: Action.Dimensions = 0; break;
			case EInputActionValueType::Axis1D: Action.Dimensions = 1; break;
			case EInputActionValueType::Axis2D: Action.Dimensions = 2; break;
			default: Action.Dimensions = 3; break;
			}
			if (Key && Key->IsValid())
			{
				Action.Keys.AddUnique(*Key);
				Action.bMouse |= IsMouseAxis(*Key);
			}
		};

		// The applied contexts aren't public, so ask the subsystem about every loaded context.
		ULocalPlayer* Player = Controller->GetLocalPlayer();
		UEnhancedInputLocalPlayerSubsystem* Subsystem = Player ? Player->GetSubsystem<UEnhancedInputLocalPlayerSubsystem>() : nullptr;
		for (TObjectIterator<UInputMappingContext> It; It && Subsystem; ++It)
		{
			if (Subsystem->HasMappingContext(*It))
			{
				for (const FEnhancedActionKeyMapping& Mapping : It->GetMappings())
				{
					AddAction(Mapping.Action, &Mapping.Key, TEXT("mapping context"));
				}
			}
		}
		// Actions the game binds in code but maps to no key are still the game's actions.
		for (UInputComponent* Component : {Controller->InputComponent.Get(), Controller->GetPawn() ? Controller->GetPawn()->InputComponent.Get() : nullptr})
		{
			if (const UEnhancedInputComponent* Enhanced = Cast<UEnhancedInputComponent>(Component))
			{
				for (const TUniquePtr<FEnhancedInputActionEventBinding>& Binding : Enhanced->GetActionEventBindings())
				{
					AddAction(Binding->GetAction(), nullptr, TEXT("input component"));
				}
			}
		}
	}
	else
	{
		DiscoverLegacy();
	}
	// Keys are known only now, so exclusion by key is decided after every discovery.
	for (FPlayAction& Action : Actions)
	{
		Action.bExcluded = IsExcluded(Action);
	}
}

void FPerfInputDriver::DiscoverLegacy()
{
	const UInputSettings* Settings = UInputSettings::GetInputSettings();
	for (const FInputActionKeyMapping& Mapping : Settings->GetActionMappings())
	{
		FPlayAction& Action = Actions[FindOrAddAction(Mapping.ActionName.ToString())];
		if (!Action.bActive)
		{
			Action.Keys.Reset();
			Action.Scales.Reset();
		}
		Action.bActive = true;
		Action.Dimensions = 0;
		Action.Source = TEXT("legacy action mapping");
		if (!Mapping.Key.IsGamepadKey() && !Action.Keys.Contains(Mapping.Key))
		{
			Action.Keys.Add(Mapping.Key);
			Action.Scales.Add(1.f);
		}
	}
	for (const FInputAxisKeyMapping& Mapping : Settings->GetAxisMappings())
	{
		FPlayAction& Action = Actions[FindOrAddAction(Mapping.AxisName.ToString())];
		if (!Action.bActive)
		{
			Action.Keys.Reset();
			Action.Scales.Reset();
			Action.bMouse = false;
		}
		Action.bActive = true;
		Action.Dimensions = 1;
		Action.Source = TEXT("legacy axis mapping");
		if (!Mapping.Key.IsGamepadKey() && !Action.Keys.Contains(Mapping.Key))
		{
			Action.Keys.Add(Mapping.Key);
			Action.Scales.Add(Mapping.Scale);
			Action.bMouse |= IsMouseAxis(Mapping.Key);
		}
	}
	for (FPlayAction& Action : Actions)
	{
		Action.bActive &= Action.Keys.Num() > 0;
	}
}

void FPerfInputDriver::StartGesture(FVoice& Voice, double Now)
{
	// Weighted pick among active, allowed actions no other voice holds. Actions
	// never fired get three times their weight, so a run covers all of them.
	float Total = 0.f;
	TArray<float, TInlineAllocator<64>> Picks;
	for (int32 i = 0; i < Actions.Num(); ++i)
	{
		const FPlayAction& Action = Actions[i];
		bool bTaken = false;
		for (const FVoice& Other : Voices)
		{
			bTaken |= &Other != &Voice && Other.Index == i;
		}
		const float W = (Action.bActive && !Action.bExcluded && !bTaken) ? Action.Weight * (Action.Fired == 0 ? 3.f : 1.f) : 0.f;
		Picks.Add(W);
		Total += W;
	}
	Voice.Index = INDEX_NONE;
	if (Total <= 0.f)
	{
		Voice.NextS = Now + 0.5;
		return;
	}
	float Roll = Random.FRandRange(0.f, Total);
	for (int32 i = 0; i < Picks.Num(); ++i)
	{
		Roll -= Picks[i];
		if (Picks[i] > 0.f && Roll <= 0.f)
		{
			Voice.Index = i;
			break;
		}
	}
	if (Voice.Index == INDEX_NONE)
	{
		Voice.Index = Picks.FindLastByPredicate([](float W) { return W > 0.f; });
	}

	FPlayAction& Action = Actions[Voice.Index];
	const float R = Random.FRand();
	double Duration = 0.0;
	if (Action.Dimensions == 0)
	{
		Voice.Gesture = R < 0.55f ? EGesture::Tap : (R < 0.7f ? EGesture::DoubleTap : EGesture::Hold);
		Duration = Voice.Gesture == EGesture::Tap ? Random.FRandRange(0.06f, 0.15f)
			: Voice.Gesture == EGesture::DoubleTap ? 0.3 : Random.FRandRange(0.4f, 1.5f);
		Voice.Dir = FVector(1.0, 0.0, 0.0);
	}
	else
	{
		Voice.Gesture = R < 0.55f ? EGesture::Direction : EGesture::Sweep;
		Duration = Random.FRandRange(0.4f, 2.0f);
		if (Action.Dimensions == 1)
		{
			Voice.Dir = FVector(Random.FRand() < 0.5f ? -1.0 : 1.0, 0.0, 0.0);
		}
		else
		{
			FVector Dir = Random.GetUnitVector();
			if (Action.Dimensions == 2)
			{
				Dir.Z = 0.0;
				Dir = Dir.IsNearlyZero() ? FVector(1.0, 0.0, 0.0) : Dir.GetSafeNormal();
			}
			Voice.Dir = Dir;
		}
		if (bLegacy)
		{
			// A legacy axis is driven through one of its keys; the key's scale sets the direction.
			Voice.Gesture = Action.bMouse ? Voice.Gesture : EGesture::Hold;
			Voice.KeyIndex = Random.RandHelper(Action.Keys.Num());
		}
	}
	Voice.StartS = Now;
	Voice.EndS = Now + Duration;
	++Action.Fired;
	Action.ActiveSeconds += Duration;
	LastAction = Action.Name;
	const FVector Shown = Action.bMouse ? Voice.Dir * MouseScale : Voice.Dir;
	Log(Action.Name, GestureName(static_cast<uint8>(Voice.Gesture)),
		bLegacy && Action.Keys.IsValidIndex(Voice.KeyIndex) ? Action.Keys[Voice.KeyIndex].ToString() : VectorText(Shown, Action.Dimensions),
		Duration * 1000.0);
}

FVector FPerfInputDriver::GestureValue(const FVoice& Voice, const FPlayAction& Action, double Now) const
{
	const double T = Now - Voice.StartS;
	FVector Value = FVector::ZeroVector;
	switch (Voice.Gesture)
	{
	case EGesture::Tap:
	case EGesture::Hold:
	case EGesture::Direction:
		Value = Voice.Dir;
		break;
	case EGesture::DoubleTap:
		// Down, up, down: 0.1 s each.
		Value = (T < 0.1 || T >= 0.2) ? Voice.Dir : FVector::ZeroVector;
		break;
	case EGesture::Sweep:
	{
		// One turn over the gesture: a sine for 1D, a circle starting at Dir for 2D and 3D.
		const double Phase = 2.0 * PI * T / FMath::Max(0.1, Voice.EndS - Voice.StartS);
		if (Action.Dimensions == 1)
		{
			Value = Voice.Dir * FMath::Sin(Phase);
		}
		else
		{
			Value = Voice.Dir.RotateAngleAxis(FMath::RadiansToDegrees(Phase), FVector::UpVector);
			Value.Z = Action.Dimensions == 3 ? Voice.Dir.Z * FMath::Cos(Phase) : 0.0;
		}
		break;
	}
	}
	return Action.bMouse ? Value * MouseScale : Value;
}

void FPerfInputDriver::LegacyKey(APlayerController* Controller, const FKey& Key, EInputEvent Event, float Amount, float DeltaTime)
{
	FViewport* Viewport = GEngine && GEngine->GameViewport ? GEngine->GameViewport->Viewport : nullptr;
	const FInputDeviceId Device = IPlatformInputDeviceMapper::Get().GetDefaultInputDevice();
	if (Event == IE_Axis)
	{
		Controller->InputKey(FInputKeyEventArgs(Viewport, Device, Key, Amount, DeltaTime, 1, FPlatformTime::Cycles64()));
	}
	else
	{
		Controller->InputKey(FInputKeyEventArgs(Viewport, Device, Key, Event, Amount, false, FPlatformTime::Cycles64()));
	}
}

void FPerfInputDriver::Inject(APlayerController* Controller, FPlayAction& Action, const FVector& Value, FVoice& Voice, float DeltaTime)
{
	if (!bLegacy)
	{
		ULocalPlayer* Player = Controller->GetLocalPlayer();
		UEnhancedInputLocalPlayerSubsystem* Subsystem = Player ? Player->GetSubsystem<UEnhancedInputLocalPlayerSubsystem>() : nullptr;
		const UInputAction* InputAction = Action.Action.Get();
		if (Subsystem && InputAction && !Value.IsNearlyZero())
		{
			// Injected each frame the gesture is down; no injection reads as released.
			Subsystem->InjectInputVectorForAction(InputAction, Value, {}, {});
		}
		return;
	}

	if (!Action.Keys.IsValidIndex(Voice.KeyIndex))
	{
		return;
	}
	const FKey& Key = Action.Keys[Voice.KeyIndex];
	if (Action.bMouse)
	{
		LegacyKey(Controller, Key, IE_Axis, static_cast<float>(Value.X), DeltaTime);
		return;
	}
	const bool bDown = !Value.IsNearlyZero();
	if (bDown != Voice.bKeyDown)
	{
		LegacyKey(Controller, Key, bDown ? IE_Pressed : IE_Released, 1.f, DeltaTime);
		Voice.bKeyDown = bDown;
	}
}

void FPerfInputDriver::ReleaseLegacy(APlayerController* Controller, FPlayAction& Action, FVoice& Voice)
{
	if (bLegacy && Voice.bKeyDown && Action.Keys.IsValidIndex(Voice.KeyIndex))
	{
		LegacyKey(Controller, Action.Keys[Voice.KeyIndex], IE_Released, 1.f, 0.f);
	}
	Voice.bKeyDown = false;
}

void FPerfInputDriver::PlayVoice(FVoice& Voice, APlayerController* Controller, double Now, float DeltaTime)
{
	if (Voice.Index != INDEX_NONE)
	{
		FPlayAction& Action = Actions[Voice.Index];
		if (Now >= Voice.EndS || !Action.bActive)
		{
			if (Controller)
			{
				ReleaseLegacy(Controller, Action, Voice);
			}
			Voice.Index = INDEX_NONE;
			Voice.NextS = Now + Random.FRandRange(0.1f, 0.6f);
		}
		else if (Controller)
		{
			Inject(Controller, Action, GestureValue(Voice, Action, Now), Voice, DeltaTime);
		}
		return;
	}
	if (Now >= Voice.NextS)
	{
		StartGesture(Voice, Now);
	}
}

bool FPerfInputDriver::Tick(float DeltaTime)
{
	const double Now = FPlatformTime::Seconds();
	UWorld* World = GameWorld();
	APlayerController* Controller = World ? World->GetFirstPlayerController() : nullptr;

	if (!CommandFile.IsEmpty() && Now - LastCommandCheckS >= CommandIntervalSeconds)
	{
		LastCommandCheckS = Now;
		ReadCommands(Controller, Now);
	}
	for (int32 i = PendingReleases.Num() - 1; i >= 0; --i)
	{
		if (Now >= PendingReleases[i].AtS)
		{
			if (PendingReleases[i].bMouse)
			{
				SlateClick(0.f, 0.f, false);
			}
			else
			{
				SlateKey(PendingReleases[i].Key, false);
			}
			PendingReleases.RemoveAt(i);
		}
	}

	if (Controller && Now - LastDiscoverS >= DiscoverIntervalSeconds)
	{
		LastDiscoverS = Now;
		Discover(Controller);
	}

	if (!bPlaying && bStartMarkerSeen && Controller)
	{
		const bool bAnyAction = Actions.ContainsByPredicate([](const FPlayAction& A) { return A.bActive && !A.bExcluded; });
		if (bAnyAction && StartAtS < 0.0)
		{
			StartAtS = Now + StartDelaySeconds;
		}
		if (StartAtS >= 0.0 && Now >= StartAtS)
		{
			bPlaying = true;
			PlayStartedS = Now;
			const int32 Allowed = Actions.FilterByPredicate([](const FPlayAction& A) { return A.bActive && !A.bExcluded; }).Num();
			Log(TEXT("<driver>"), TEXT("start"), FString::Printf(TEXT("%d allowed of %d actions"), Allowed, Actions.Num()), 0.0);
		}
	}

	if (bPlaying && PlaySeconds > 0.0 && Now - PlayStartedS >= PlaySeconds)
	{
		bQuitRequested = true;
	}

	if (bPlaying && !bPaused)
	{
		for (FVoice& Voice : Voices)
		{
			PlayVoice(Voice, Controller, Now, DeltaTime);
		}
	}
	else if (Voices[0].Index != INDEX_NONE)
	{
		// An action the agent fired while the driver is stopped.
		PlayVoice(Voices[0], Controller, Now, DeltaTime);
	}

	if (Now - LastStatusS >= StatusIntervalSeconds)
	{
		LastStatusS = Now;
		WriteStatus(Controller, Now);
	}
	return true;
}

void FPerfInputDriver::ReadCommands(APlayerController* Controller, double Now)
{
	if (IFileManager::Get().FileSize(*CommandFile) <= 0)
	{
		return;
	}
	TArray<FString> Lines;
	if (!FFileHelper::LoadFileToStringArray(Lines, *CommandFile))
	{
		return;
	}
	for (int32 i = CommandLinesRead; i < Lines.Num(); ++i)
	{
		const FString Line = Lines[i].TrimStartAndEnd();
		if (!Line.IsEmpty())
		{
			RunCommand(Line, Controller, Now);
		}
	}
	CommandLinesRead = FMath::Max(CommandLinesRead, Lines.Num());
}

void FPerfInputDriver::RunCommand(const FString& Line, APlayerController* Controller, double Now)
{
	FString Verb, Rest;
	if (!Line.Split(TEXT(" "), &Verb, &Rest))
	{
		Verb = Line;
	}
	Verb = Verb.ToLower();
	TArray<FString> Args;
	Rest.ParseIntoArrayWS(Args);
	LastCommand = Line;
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: agent command '%s'"), *Line);
	Log(TEXT("<agent>"), Verb, Rest, 0.0);

	if (Verb == TEXT("pause"))
	{
		bPaused = true;
		for (FVoice& Voice : Voices)
		{
			if (Voice.Index != INDEX_NONE && Controller)
			{
				ReleaseLegacy(Controller, Actions[Voice.Index], Voice);
			}
			Voice.Index = INDEX_NONE;
		}
	}
	else if (Verb == TEXT("resume"))
	{
		bPaused = false;
		if (!bPlaying)
		{
			bPlaying = true;
			PlayStartedS = Now;
		}
	}
	else if (Verb == TEXT("quit"))
	{
		bQuitRequested = true;
		bAgentQuit = true;
	}
	else if (Verb == TEXT("key") && Args.Num() >= 1)
	{
		const FKey Key(*Args[0]);
		if (Key.IsValid())
		{
			SlateKey(Key, true);
			PendingReleases.Add({Now + (Args.Num() > 1 ? FCString::Atod(*Args[1]) : 0.1), Key, false});
		}
	}
	else if (Verb == TEXT("click") && Args.Num() >= 2)
	{
		SlateClick(FCString::Atof(*Args[0]), FCString::Atof(*Args[1]), true);
		PendingReleases.Add({Now + 0.1, EKeys::LeftMouseButton, true});
	}
	else if (Verb == TEXT("type") && FSlateApplication::IsInitialized())
	{
		for (const TCHAR Char : Rest)
		{
			FSlateApplication::Get().ProcessKeyCharEvent(FCharacterEvent(Char, FModifierKeysState(), 0, false));
		}
	}
	else if (Verb == TEXT("exec") && GEngine)
	{
		GEngine->Exec(GameWorld(), *Rest);
	}
	else if (Verb == TEXT("action") && Args.Num() >= 1)
	{
		const int32 Index = Actions.IndexOfByPredicate([&Args](const FPlayAction& A) { return A.Name == Args[0]; });
		if (Index != INDEX_NONE && Actions[Index].bActive)
		{
			FVoice& Voice = Voices[0];
			if (Voice.Index != INDEX_NONE && Controller)
			{
				ReleaseLegacy(Controller, Actions[Voice.Index], Voice);
			}
			Voice.Index = Index;
			Voice.Gesture = EGesture::Hold;
			Voice.Dir = FVector(1.0, 0.0, 0.0);
			Voice.StartS = Now;
			Voice.EndS = Now + (Args.Num() > 1 ? FCString::Atod(*Args[1]) : 0.15);
			++Actions[Index].Fired;
			LastAction = Actions[Index].Name;
			Log(Actions[Index].Name, TEXT("hold"), TEXT("1.00"), (Voice.EndS - Now) * 1000.0);
		}
	}
}

void FPerfInputDriver::SlateKey(const FKey& Key, bool bDown)
{
	if (!FSlateApplication::IsInitialized())
	{
		return;
	}
	const FKeyEvent Event(Key, FModifierKeysState(), 0, false, 0, 0);
	if (bDown)
	{
		FSlateApplication::Get().ProcessKeyDownEvent(Event);
	}
	else
	{
		FSlateApplication::Get().ProcessKeyUpEvent(Event);
	}
}

void FPerfInputDriver::SlateClick(float X, float Y, bool bDown)
{
	if (!FSlateApplication::IsInitialized() || !GEngine || !GEngine->GameViewport)
	{
		return;
	}
	TSharedPtr<SWindow> Window = GEngine->GameViewport->GetWindow();
	if (!Window.IsValid())
	{
		return;
	}
	FSlateApplication& App = FSlateApplication::Get();
	static FVector2D ClickPos = FVector2D::ZeroVector;
	if (bDown)
	{
		const FSlateRect Rect = Window->GetClientRectInScreen();
		ClickPos = FVector2D(Rect.Left + X * (Rect.Right - Rect.Left), Rect.Top + Y * (Rect.Bottom - Rect.Top));
		App.SetCursorPos(ClickPos);
		const TSet<FKey> None;
		App.ProcessMouseMoveEvent(FPointerEvent(0, FSlateApplication::CursorPointerIndex, ClickPos, ClickPos, None, EKeys::Invalid, 0.f, FModifierKeysState()));
		const TSet<FKey> Pressed = {EKeys::LeftMouseButton};
		App.ProcessMouseButtonDownEvent(Window->GetNativeWindow(),
			FPointerEvent(0, FSlateApplication::CursorPointerIndex, ClickPos, ClickPos, Pressed, EKeys::LeftMouseButton, 0.f, FModifierKeysState()));
	}
	else
	{
		const TSet<FKey> None;
		App.ProcessMouseButtonUpEvent(
			FPointerEvent(0, FSlateApplication::CursorPointerIndex, ClickPos, ClickPos, None, EKeys::LeftMouseButton, 0.f, FModifierKeysState()));
	}
}

void FPerfInputDriver::WriteStatus(APlayerController* Controller, double Now)
{
	UWorld* World = GameWorld();
	int32 Found = 0, Allowed = 0, Covered = 0;
	for (const FPlayAction& Action : Actions)
	{
		Found += Action.bActive ? 1 : 0;
		Allowed += Action.bActive && !Action.bExcluded ? 1 : 0;
		Covered += !Action.bExcluded && Action.Fired > 0 ? 1 : 0;
	}
	const FString Status = FString::Printf(
		TEXT("{\"t_ms\": %.0f, \"frame\": %llu, \"map\": %s, \"pawn\": %s, \"paused_game\": %s, \"cursor\": %s, ")
		TEXT("\"driver\": \"%s\", \"input\": \"%s\", \"actions_found\": %d, \"actions_allowed\": %d, \"actions_fired\": %d, ")
		TEXT("\"last_action\": %s, \"last_command\": %s, \"fps\": %.1f}\n"),
		Recorder.NowMs(), Recorder.FrameIndex(),
		*Quoted(World ? World->GetMapName() : FString()),
		*Quoted(Controller && Controller->GetPawn() ? Controller->GetPawn()->GetClass()->GetName() : FString()),
		World && World->IsPaused() ? TEXT("true") : TEXT("false"),
		Controller && Controller->ShouldShowMouseCursor() ? TEXT("true") : TEXT("false"),
		bPaused ? TEXT("paused") : (bPlaying ? TEXT("playing") : TEXT("waiting")),
		bLegacy ? TEXT("legacy") : TEXT("enhanced"),
		Found, Allowed, Covered, *Quoted(LastAction), *Quoted(LastCommand),
		FApp::GetDeltaTime() > 0.0 ? 1.0 / FApp::GetDeltaTime() : 0.0);
	const FString Path = FPaths::Combine(OutputFolder, TEXT("status.json"));
	// Off the game thread, so the status file never costs the capture a frame.
	AsyncTask(ENamedThreads::AnyBackgroundThreadNormalTask, [Path, Status]()
	{
		FFileHelper::SaveStringToFile(Status, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
	});
}

void FPerfInputDriver::Log(const FString& Action, const FString& Gesture, const FString& Value, double DurationMs)
{
	Rows.Add({Recorder.FrameIndex(), Recorder.NowMs(), Action, Gesture, Value, DurationMs});
}

void FPerfInputDriver::Write() const
{
	FString Csv = TEXT("frame,t_ms,action,gesture,value,duration_ms\n");
	for (const FInputRow& Row : Rows)
	{
		Csv += FString::Printf(TEXT("%llu,%.3f,%s,%s,%s,%.1f\n"), Row.Frame, Row.TimeMs, *Quoted(Row.Action),
			*Row.Gesture, *Quoted(Row.Value), Row.DurationMs);
	}
	FFileHelper::SaveStringToFile(Csv, *FPaths::Combine(OutputFolder, TEXT("inputs.csv")),
		FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);

	FString ActionCsv = FString::Printf(TEXT("# seed=%d input=%s\nname,value_type,keys,source,excluded,weight,fired,active_s\n"),
		Seed, bLegacy ? TEXT("legacy") : TEXT("enhanced"));
	for (const FPlayAction& Action : Actions)
	{
		TArray<FString> KeyNames;
		for (const FKey& Key : Action.Keys)
		{
			KeyNames.Add(Key.ToString());
		}
		static const TCHAR* Types[] = {TEXT("bool"), TEXT("axis1d"), TEXT("axis2d"), TEXT("axis3d")};
		ActionCsv += FString::Printf(TEXT("%s,%s,%s,%s,%d,%.2f,%d,%.2f\n"), *Quoted(Action.Name), Types[Action.Dimensions],
			*Quoted(FString::Join(KeyNames, TEXT(" "))), *Quoted(Action.Source), Action.bExcluded ? 1 : 0, Action.Weight,
			Action.Fired, Action.ActiveSeconds);
	}
	FFileHelper::SaveStringToFile(ActionCsv, *FPaths::Combine(OutputFolder, TEXT("actions.csv")),
		FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
}
