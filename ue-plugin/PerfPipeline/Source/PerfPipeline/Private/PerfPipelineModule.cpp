#include "PerfPipelineModule.h"

#include "PerfFrameRecorder.h"
#include "Misc/App.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "ProfilingDebugging/CsvProfilerConfig.h"
#include "ProfilingDebugging/MiscTrace.h"
#include "Trace/Trace.h"

void FPerfPipelineModule::StartupModule()
{
	FString OutputFolder;
	if (!FParse::Value(FCommandLine::Get(), TEXT("-PerfOut="), OutputFolder))
	{
		return;
	}
	OutputFolder = FPaths::ConvertRelativePathToFull(OutputFolder);

	FParse::Value(FCommandLine::Get(), TEXT("-PerfQuitAfter="), QuitAfterSeconds);
	StartSeconds = FPlatformTime::Seconds();

	Recorder = MakeUnique<FPerfFrameRecorder>(OutputFolder);

#if UE_TRACE_ENABLED
	const int32 bTracing = UE::Trace::IsTracing() ? 1 : 0;
#else
	const int32 bTracing = 0;
#endif

	// What this build can record, so the pipeline can check a capture is usable.
	const FString Info = FString::Printf(
		TEXT("build_config=%s\nue_trace_enabled=%d\ntracing=%d\ncsv_profiler=%d\nstats=%d\ncommand_line=%s\n"),
		LexToString(FApp::GetBuildConfiguration()),
		UE_TRACE_ENABLED,
		bTracing,
		CSV_PROFILER,
		STATS,
		FCommandLine::Get());
	FFileHelper::SaveStringToFile(Info, *FPaths::Combine(OutputFolder, TEXT("run_info.txt")));

	TRACE_BOOKMARK(TEXT("run_start"));
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: run_start, writing to %s"), *OutputFolder);

	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FPerfPipelineModule::Tick));
}

void FPerfPipelineModule::ShutdownModule()
{
	FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	if (Recorder.IsValid())
	{
		TRACE_BOOKMARK(TEXT("run_end"));
		Recorder.Reset();
	}
}

bool FPerfPipelineModule::Tick(float DeltaTime)
{
	if (QuitAfterSeconds > 0.0 && !bQuitRequested && FPlatformTime::Seconds() - StartSeconds >= QuitAfterSeconds)
	{
		bQuitRequested = true;
		TRACE_BOOKMARK(TEXT("run_end"));
		Recorder.Reset();
		UE_LOG(LogTemp, Log, TEXT("PerfPipeline: run_end, quitting"));
		FPlatformMisc::RequestExit(false, TEXT("PerfPipeline"));
	}
	return true;
}

IMPLEMENT_MODULE(FPerfPipelineModule, PerfPipeline)
