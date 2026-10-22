#pragma once

#include "Containers/Ticker.h"
#include "Modules/ModuleInterface.h"
#include "Templates/UniquePtr.h"

class FPerfFrameRecorder;

/**
 * Entry point. Does nothing unless the game is started with -PerfOut=<folder>.
 *
 * Command line:
 *   -PerfOut=<folder>       write frames.csv and run_info.txt to this folder
 *   -PerfQuitAfter=<sec>    quit the game this many seconds after startup
 */
class FPerfPipelineModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

private:
	bool Tick(float DeltaTime);

	TUniquePtr<FPerfFrameRecorder> Recorder;
	FTSTicker::FDelegateHandle TickHandle;
	double StartSeconds = 0.0;
	double QuitAfterSeconds = 0.0;
	bool bQuitRequested = false;
};
