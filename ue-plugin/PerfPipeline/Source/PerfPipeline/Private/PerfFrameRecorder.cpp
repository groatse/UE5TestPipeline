#include "PerfFrameRecorder.h"

#include "DynamicRHI.h"
#include "HAL/PlatformFileManager.h"
#include "Misc/App.h"
#include "Misc/CoreDelegates.h"
#include "Misc/Paths.h"
#include "RenderTimer.h"

namespace
{
	constexpr int32 RowsPerFlush = 120;

	double CyclesToMs(uint32 Cycles)
	{
		return FPlatformTime::ToMilliseconds(Cycles);
	}
}

FPerfFrameRecorder::FPerfFrameRecorder(const FString& OutputFolder)
{
	IPlatformFile& PlatformFile = FPlatformFileManager::Get().GetPlatformFile();
	PlatformFile.CreateDirectoryTree(*OutputFolder);

	const FString Path = FPaths::Combine(OutputFolder, TEXT("frames.csv"));
	File.Reset(PlatformFile.OpenWrite(*Path));
	if (!File.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("PerfPipeline: could not open %s"), *Path);
		return;
	}

	Pending = TEXT("frame,t_ms,frame_ms,game_ms,render_ms,rhi_ms,gpu_ms\n");
	StartSeconds = FPlatformTime::Seconds();
	FirstFrame = GFrameCounter;
	EndFrameHandle = FCoreDelegates::OnEndFrame.AddRaw(this, &FPerfFrameRecorder::OnEndFrame);
}

FPerfFrameRecorder::~FPerfFrameRecorder()
{
	FCoreDelegates::OnEndFrame.Remove(EndFrameHandle);
	Flush();
}

void FPerfFrameRecorder::OnEndFrame()
{
	// The thread and GPU values describe the most recently completed frame.
	Pending += FString::Printf(TEXT("%llu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n"),
		GFrameCounter - FirstFrame,
		(FPlatformTime::Seconds() - StartSeconds) * 1000.0,
		FApp::GetDeltaTime() * 1000.0,
		CyclesToMs(GGameThreadTime),
		CyclesToMs(GRenderThreadTime),
		CyclesToMs(GRHIThreadTime),
		CyclesToMs(RHIGetGPUFrameCycles(0)));

	if (++PendingRows >= RowsPerFlush)
	{
		Flush();
	}
}

void FPerfFrameRecorder::Flush()
{
	if (File.IsValid() && !Pending.IsEmpty())
	{
		const FTCHARToUTF8 Utf8(*Pending);
		File->Write(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length());
		File->Flush();
	}
	Pending.Reset();
	PendingRows = 0;
}
