#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "InputCoreTypes.h"
#include "Math/RandomStream.h"
#include "UObject/WeakObjectPtr.h"

class APlayerController;
class FPerfFrameRecorder;
class UInputAction;
class UWorld;

/**
 * Plays the game by firing the player's own bound input actions. Started with
 * -PerfPlay=<file>, a JSON object written from the game profile:
 *
 *   {
 *     "start_on": "pawn_possessed",   marker that starts play; empty starts once actions are found
 *     "start_delay_s": 1.0,           wait after that before the first action
 *     "exclude": ["*Voice*"],         action names (wildcards) never fired
 *     "exclude_keys": ["Escape"],     actions mapped to any of these keys are never fired
 *     "weights": {"LookAction": 3},   relative pick weight, default 1
 *     "voices": 2,                    actions played at the same time
 *     "mouse_scale": 5.0,             per-frame delta for actions mapped to a mouse axis
 *     "seed": 0,                      0 picks one; the seed is written to actions.csv
 *     "play_s": 300,                  quit after this long playing; 0 plays until something else ends the run
 *     "commands": "<file>"            agent command file, read while the game runs
 *   }
 *
 * Actions are found without game code. With Enhanced Input, every action in the
 * player's active mapping contexts plus every action bound on the player
 * controller's and pawn's input components, re-read each second so new pawns and
 * contexts are picked up, and fired with InjectInputForAction. Without Enhanced
 * Input, the legacy action and axis mappings, fired as key events.
 *
 * Buttons get taps, double taps and holds; 1D axes get held directions and
 * sweeps; 2D and 3D axes get held directions and circular sweeps. Picks are
 * random with the profile's weights, and actions not fired yet are favoured so
 * that every allowed action fires during a run.
 *
 * The agent supervising the run appends lines to the command file:
 *   pause | resume              stop or restart the driver
 *   key <Key> [hold_s]          press a key through Slate (menus, dialogs)
 *   click <x> <y>               left click at a point of the game window, 0..1
 *   type <text>                 type characters through Slate
 *   action <Name> [hold_s]      fire one action
 *   exec <console command>      run a console command
 *   quit                        end the run
 *
 * Writes inputs.csv (one row per gesture or command, with frame and time on the
 * frame recorder's clock), actions.csv (every action found, with coverage) and,
 * once a second from a worker thread, status.json for the agent.
 */
class FPerfInputDriver
{
public:
	FPerfInputDriver(FPerfFrameRecorder& InRecorder, const FString& ConfigFile, const FString& InOutputFolder);
	~FPerfInputDriver();

	void OnMarker(const FString& Name);
	bool QuitRequested() const { return bQuitRequested; }
	/** Why the driver asked to quit: "play_time" or "agent". */
	const TCHAR* QuitReason() const { return bAgentQuit ? TEXT("agent") : TEXT("play_time"); }

private:
	enum class EGesture : uint8 { Tap, DoubleTap, Hold, Direction, Sweep };

	struct FPlayAction
	{
		FString Name;
		TWeakObjectPtr<const UInputAction> Action;  // Enhanced Input; null for legacy mappings
		int32 Dimensions = 0;                        // 0 for a button, else 1 to 3 axes
		TArray<FKey> Keys;                           // mapped keys, legacy keys carry Scales
		TArray<float> Scales;
		bool bMouse = false;                         // mapped to a mouse axis: values are per-frame deltas
		bool bExcluded = false;
		bool bActive = false;                        // present in the latest discovery
		float Weight = 1.f;
		FString Source;
		int32 Fired = 0;
		double ActiveSeconds = 0.0;
	};

	struct FVoice
	{
		int32 Index = INDEX_NONE;
		EGesture Gesture = EGesture::Tap;
		double StartS = 0.0;
		double EndS = 0.0;
		double NextS = 0.0;
		FVector Dir = FVector::ZeroVector;
		int32 KeyIndex = 0;
		bool bKeyDown = false;
	};

	struct FInputRow
	{
		uint64 Frame;
		double TimeMs;
		FString Action;
		FString Gesture;
		FString Value;
		double DurationMs;
	};

	struct FPendingRelease
	{
		double AtS;
		FKey Key;
		bool bMouse;
	};

	void LoadConfig(const FString& ConfigFile);
	bool Tick(float DeltaTime);
	UWorld* GameWorld() const;
	void Discover(APlayerController* Controller);
	void DiscoverLegacy();
	int32 FindOrAddAction(const FString& Name);
	bool IsExcluded(const FPlayAction& Action) const;

	void StartGesture(FVoice& Voice, double Now);
	void PlayVoice(FVoice& Voice, APlayerController* Controller, double Now, float DeltaTime);
	FVector GestureValue(const FVoice& Voice, const FPlayAction& Action, double Now) const;
	void Inject(APlayerController* Controller, FPlayAction& Action, const FVector& Value, FVoice& Voice, float DeltaTime);
	void ReleaseLegacy(APlayerController* Controller, FPlayAction& Action, FVoice& Voice);
	void LegacyKey(APlayerController* Controller, const FKey& Key, EInputEvent Event, float Amount, float DeltaTime);

	void ReadCommands(APlayerController* Controller, double Now);
	void RunCommand(const FString& Line, APlayerController* Controller, double Now);
	void SlateKey(const FKey& Key, bool bDown);
	void SlateClick(float X, float Y, bool bDown);
	void WriteStatus(APlayerController* Controller, double Now);
	void Log(const FString& Action, const FString& Gesture, const FString& Value, double DurationMs);
	void Write() const;

	FPerfFrameRecorder& Recorder;
	FString OutputFolder;
	FString CommandFile;
	FString StartOn;
	double StartDelaySeconds = 1.0;
	TArray<FString> Exclude;
	TArray<FString> ExcludeKeys;
	TMap<FString, float> Weights;
	int32 VoiceCount = 2;
	float MouseScale = 5.f;
	int32 Seed = 0;
	FRandomStream Random;

	TArray<FPlayAction> Actions;
	TArray<FVoice> Voices;
	TArray<FInputRow> Rows;
	TArray<FPendingRelease> PendingReleases;
	TArray<FString> MarkersSeen;

	bool bLegacy = false;
	bool bStartMarkerSeen = false;
	bool bPlaying = false;
	bool bPaused = false;
	bool bQuitRequested = false;
	bool bAgentQuit = false;
	double PlaySeconds = 0.0;
	double PlayStartedS = 0.0;
	double StartAtS = -1.0;
	double LastDiscoverS = -10.0;
	double LastCommandCheckS = 0.0;
	double LastStatusS = 0.0;
	int32 CommandLinesRead = 0;
	FString LastAction;
	FString LastCommand;
	FTSTicker::FDelegateHandle TickHandle;
};
