#include "PerfPipelineModule.h"

#include "PerfFrameRecorder.h"
#include "PerfInputDriver.h"
#include "PerfLifecycle.h"
#include "Misc/App.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "ProfilingDebugging/CsvProfilerConfig.h"
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
	FString EventsFile, QuitOn, PlayFile;
	FParse::Value(FCommandLine::Get(), TEXT("-PerfEvents="), EventsFile);
	FParse::Value(FCommandLine::Get(), TEXT("-PerfPlay="), PlayFile);
	FParse::Value(FCommandLine::Get(), TEXT("-PerfQuitOn="), QuitOn);
	StartSeconds = FPlatformTime::Seconds();
	// How long the process ran before recording began: the head of the launch-to-playable time.
	const double LaunchToRecordingSeconds = StartSeconds - GStartTime;

	Recorder = MakeUnique<FPerfFrameRecorder>(OutputFolder);

	// Emit verbose stat scopes as named events too, so the trace names more of the
	// engine's per-object work. Only matters with -statnamedevents.
	GShouldEmitVerboseNamedEvents = true;

#if UE_TRACE_ENABLED
	const int32 bTracing = UE::Trace::IsTracing() ? 1 : 0;
#else
	const int32 bTracing = 0;
#endif

	// What this build can record, so the pipeline can check a capture is usable.
	const FString Info = FString::Printf(
		TEXT("build_config=%s\nlaunch_to_recording_s=%.3f\nue_trace_enabled=%d\ntracing=%d\ncsv_profiler=%d\nstats=%d\ncommand_line=%s\n"),
		LexToString(FApp::GetBuildConfiguration()),
		LaunchToRecordingSeconds,
		UE_TRACE_ENABLED,
		bTracing,
		CSV_PROFILER,
		STATS,
		FCommandLine::Get());
	FFileHelper::SaveStringToFile(Info, *FPaths::Combine(OutputFolder, TEXT("run_info.txt")));

	Lifecycle = MakeUnique<FPerfLifecycle>(*Recorder, FPaths::ConvertRelativePathToFull(EventsFile), QuitOn);
	if (!PlayFile.IsEmpty())
	{
		Driver = MakeUnique<FPerfInputDriver>(*Recorder, FPaths::ConvertRelativePathToFull(PlayFile), OutputFolder);
		Lifecycle->OnMarker.AddRaw(Driver.Get(), &FPerfInputDriver::OnMarker);
	}
	UE_LOG(LogTemp, Log, TEXT("PerfPipeline: recording to %s"), *OutputFolder);

	TickHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FPerfPipelineModule::Tick));
}

void FPerfPipelineModule::ShutdownModule()
{
	FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
	StopRecording();
}

void FPerfPipelineModule::StopRecording()
{
	// The driver and the lifecycle write through the recorder's clock, so they go first.
	Driver.Reset();
	Lifecycle.Reset();
	Recorder.Reset();
}

bool FPerfPipelineModule::Tick(float DeltaTime)
{
	if (bQuitRequested || !Recorder.IsValid())
	{
		return true;
	}
	const bool bTimeUp = QuitAfterSeconds > 0.0 && FPlatformTime::Seconds() - StartSeconds >= QuitAfterSeconds;
	const bool bMarker = Lifecycle.IsValid() && Lifecycle->QuitRequested();
	const bool bDriver = Driver.IsValid() && Driver->QuitRequested();
	if (bTimeUp || bMarker || bDriver)
	{
		bQuitRequested = true;
		const TCHAR* Why = bDriver ? Driver->QuitReason() : (bMarker ? TEXT("quit marker") : TEXT("time up"));
		UE_LOG(LogTemp, Log, TEXT("PerfPipeline: quitting (%s)"), Why);
		// How the run ended, for the launcher and the report.
		FFileHelper::SaveStringToFile(FString::Printf(TEXT("ended=%s\n"), Why), *FPaths::Combine(Recorder->OutputFolderPath(), TEXT("run_end.txt")));
		StopRecording();
		FPlatformMisc::RequestExit(false, TEXT("PerfPipeline"));
	}
	return true;
}

IMPLEMENT_MODULE(FPerfPipelineModule, PerfPipeline)
