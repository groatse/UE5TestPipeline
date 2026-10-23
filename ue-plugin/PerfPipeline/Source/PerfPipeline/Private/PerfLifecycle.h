#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "Engine/World.h"

class APawn;
class FPerfFrameRecorder;
class FPerfLogWatcher;

/**
 * Fires lifecycle markers without any game code.
 *
 * Engine events are always hooked: map load start and end, player login,
 * pawn possessed, seamless travel and AGameMode match state.
 * Game-specific events come from a JSON file passed with -PerfEvents=<file>:
 *
 *   [
 *     {"name": "match_end", "poll": "GameMode.IsMatchEnded"},
 *     {"name": "dice_spawned", "spawn": "BP_DiceActor_C"},
 *     {"name": "summary", "log": "GAME SUMMARY"}
 *   ]
 *
 * "poll" names an object (GameMode, GameState, PlayerController or Pawn) and a
 * bool property or a parameterless bool function on it; the marker fires when
 * it turns true. "spawn" fires when an actor of that class, or a subclass with
 * that name in its chain, is spawned. "log" fires when a log line contains the
 * text. Entries with "scope" are read from the trace by the analyzer instead.
 * Each entry fires once unless it has "repeat": true.
 *
 * Every marker is written to markers.csv and as an Insights bookmark, and
 * -PerfQuitOn=<marker name> quits the game when that marker fires.
 */
class FPerfLifecycle
{
public:
	FPerfLifecycle(FPerfFrameRecorder& InRecorder, const FString& EventsFile, const FString& InQuitOn);
	~FPerfLifecycle();

	void Fire(const FString& Name, const FString& Detail = FString());

	/** Broadcast with the marker name after each marker is recorded. */
	TMulticastDelegate<void(const FString&)> OnMarker;
	bool QuitRequested() const { return bQuitRequested; }

private:
	struct FGameEvent
	{
		FString Name;
		FString PollObject;
		FString PollMember;
		FString SpawnClass;
		FString LogText;
		bool bRepeat = false;
		bool bFired = false;
		bool bLastPoll = false;
	};

	void LoadEvents(const FString& EventsFile);
	bool Tick(float DeltaTime);
	void PollEvents();
	bool ReadBool(UObject* Object, const FString& Member) const;
	UObject* PollTarget(const FString& Which) const;
	UWorld* GameWorld() const;

	void OnPreLoadMap(const FString& MapName);
	void OnPostLoadMap(UWorld* World);
	void OnPostLogin(AGameModeBase* GameMode, APlayerController* Player);
	void OnMatchStateSet(FName MatchState);
	void OnSeamlessTravel(UWorld* World, const FString& MapName);
	void OnActorSpawned(AActor* Actor);
	void HookWorld(UWorld* World);

	FPerfFrameRecorder& Recorder;
	TArray<FGameEvent> Events;
	FString QuitOn;
	bool bQuitRequested = false;
	double LastPollSeconds = 0.0;

	TUniquePtr<FPerfLogWatcher> LogWatcher;
	TWeakObjectPtr<UWorld> HookedWorld;
	FDelegateHandle SpawnHandle;
	TArray<TPair<TWeakObjectPtr<APlayerController>, FDelegateHandle>> PawnHooks;
	FDelegateHandle PreLoadHandle, PostLoadHandle, LoginHandle, MatchStateHandle, TravelHandle;
	FTSTicker::FDelegateHandle TickHandle;
};
