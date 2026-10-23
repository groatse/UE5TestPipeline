#pragma once

#include "Containers/Ticker.h"
#include "Modules/ModuleInterface.h"
#include "Templates/UniquePtr.h"

class FPerfFrameRecorder;
class FPerfLifecycle;

/**
 * Entry point. Does nothing unless the game is started with -PerfOut=<folder>.
 *
 * Command line:
 *   -PerfOut=<folder>       write frames.csv, markers.csv and run_info.txt to this folder
 *   -PerfQuitAfter=<sec>    quit the game this many seconds after startup
 *   -PerfEvents=<file>      JSON list of game-specific lifecycle events (see FPerfLifecycle)
 *   -PerfQuitOn=<marker>    quit the game when this lifecycle marker fires
 */
class FPerfPipelineModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

private:
	bool Tick(float DeltaTime);
	void StopRecording();

	TUniquePtr<FPerfFrameRecorder> Recorder;
	TUniquePtr<FPerfLifecycle> Lifecycle;
	FTSTicker::FDelegateHandle TickHandle;
	double StartSeconds = 0.0;
	double QuitAfterSeconds = 0.0;
	bool bQuitRequested = false;
};
