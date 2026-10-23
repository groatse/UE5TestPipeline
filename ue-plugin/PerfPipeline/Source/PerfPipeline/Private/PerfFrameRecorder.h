#pragma once

#include "CoreMinimal.h"

/**
 * Records one row per frame from the engine's frame timing values and writes
 * them as CSV when recording stops. Rows stay in memory during the run so the
 * recorder never touches the disk on the game thread mid-capture.
 */
class FPerfFrameRecorder
{
public:
	explicit FPerfFrameRecorder(const FString& InOutputFolder);
	~FPerfFrameRecorder();

	/** Record a lifecycle marker on the current frame; written to markers.csv. Game thread only. */
	void AddMarker(const FString& Name, const FString& Detail);

	/** Time and frame on the recorder's clock, for other logs that line up with frames.csv. */
	double NowMs() const { return (FPlatformTime::Seconds() - StartSeconds) * 1000.0; }
	uint64 FrameIndex() const { return GFrameCounter - FirstFrame; }
	const FString& OutputFolderPath() const { return OutputFolder; }

private:
	struct FMarker
	{
		uint64 Frame;
		double TimeMs;
		FString Name;
		FString Detail;
	};

	TArray<FMarker> Markers;

	struct FRow
	{
		uint64 Frame;
		double TimeMs;
		float FrameMs;
		float GameMs;
		float RenderMs;
		float RhiMs;
		float GpuMs;
	};

	void OnEndFrame();
	void Write() const;

	FString OutputFolder;
	TArray<FRow> Rows;
	FDelegateHandle EndFrameHandle;
	double StartSeconds = 0.0;
	uint64 FirstFrame = 0;
};
