#include "PerfFrameRecorder.h"

#include "DynamicRHI.h"
#include "HAL/PlatformFileManager.h"
#include "Misc/App.h"
#include "Misc/CoreDelegates.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "RenderTimer.h"

namespace
{
	// About 10 minutes at 90 Hz, so the array never grows during a normal run.
	constexpr int32 ReservedRows = 60 * 1000;

	float CyclesToMs(uint32 Cycles)
	{
		return static_cast<float>(FPlatformTime::ToMilliseconds(Cycles));
	}
}

FPerfFrameRecorder::FPerfFrameRecorder(const FString& InOutputFolder)
	: OutputFolder(InOutputFolder)
{
	Rows.Reserve(ReservedRows);
	StartSeconds = FPlatformTime::Seconds();
	FirstFrame = GFrameCounter;
	EndFrameHandle = FCoreDelegates::OnEndFrame.AddRaw(this, &FPerfFrameRecorder::OnEndFrame);
}

FPerfFrameRecorder::~FPerfFrameRecorder()
{
	FCoreDelegates::OnEndFrame.Remove(EndFrameHandle);
	Write();
}

void FPerfFrameRecorder::OnEndFrame()
{
	// The thread and GPU values describe the most recently completed frame.
	Rows.Add({
		GFrameCounter - FirstFrame,
		(FPlatformTime::Seconds() - StartSeconds) * 1000.0,
		static_cast<float>(FApp::GetDeltaTime() * 1000.0),
		CyclesToMs(GGameThreadTime),
		CyclesToMs(GRenderThreadTime),
		CyclesToMs(GRHIThreadTime),
		CyclesToMs(RHIGetGPUFrameCycles(0))});
}

void FPerfFrameRecorder::Write() const
{
	FString Csv;
	Csv.Reserve(Rows.Num() * 64);
	Csv += TEXT("frame,t_ms,frame_ms,game_ms,render_ms,rhi_ms,gpu_ms\n");
	for (const FRow& Row : Rows)
	{
		Csv += FString::Printf(TEXT("%llu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n"),
			Row.Frame, Row.TimeMs, Row.FrameMs, Row.GameMs, Row.RenderMs, Row.RhiMs, Row.GpuMs);
	}

	FPlatformFileManager::Get().GetPlatformFile().CreateDirectoryTree(*OutputFolder);
	const FString Path = FPaths::Combine(OutputFolder, TEXT("frames.csv"));
	if (!FFileHelper::SaveStringToFile(Csv, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		UE_LOG(LogTemp, Error, TEXT("PerfPipeline: could not write %s"), *Path);
	}
}
