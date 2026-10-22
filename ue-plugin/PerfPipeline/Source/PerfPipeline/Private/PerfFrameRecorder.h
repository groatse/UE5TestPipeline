#pragma once

#include "CoreMinimal.h"

class IFileHandle;

/**
 * Writes one CSV row per frame from the engine's frame timing values.
 * These are set in every build configuration, Shipping included, so this
 * works without the CSV profiler or stats.
 */
class FPerfFrameRecorder
{
public:
	explicit FPerfFrameRecorder(const FString& OutputFolder);
	~FPerfFrameRecorder();

	bool IsOpen() const { return File.IsValid(); }

private:
	void OnEndFrame();
	void Flush();

	TUniquePtr<IFileHandle> File;
	FDelegateHandle EndFrameHandle;
	FString Pending;
	int32 PendingRows = 0;
	double StartSeconds = 0.0;
	uint64 FirstFrame = 0;
};
