# UE5TestPipeline

Automated performance playtests for UE5 projects. The pipeline plays a packaged build by firing the player's own input actions, records frame timing and an Unreal Insights trace, and reports what caused each hitch and budget overrun and where the biggest gains are.

The first project under test is LoadedDiceFaceOff (UE 5.7).

## Status

Milestones 1 and 2 are done: the plugin records per-frame timing in a packaged Development build alongside a full Insights trace, and `ueperf analyze` finds sustained overruns and hitches in it.

| Milestone | State |
| --- | --- |
| 1. Capture spike | Done |
| 2. Frame analysis | Done |
| 3. Cause finding and gains ranking | Not started |
| 4. Single-run report | Not started |
| 5. Lifecycle markers and capture control | Not started |
| 6. Input driver | Not started |
| 7. Onboarding, launcher and supervision | Not started |
| 8. Results store | Not started |

## Requirements

- UE 5.7 installed from the Epic launcher. A custom-built engine is not supported.
- Visual Studio 2022 with the UE C++ workloads.
- Python 3.11 or newer, with pytest for the tests.

## Build configuration

Captures use packaged **Development** builds. The launcher engine can't build Shipping with trace, because that needs a unique build environment, and it ships no precompiled Test libraries. Development builds carry some overhead that Shipping doesn't, so absolute frame times read slightly high, but the ranking of causes holds.

## Repo layout

```
ue-plugin/PerfPipeline/   UE plugin: frame recorder and capture control
ueperf/                   Python package: ingest, analyzer, command line
  thresholds.toml         default analyzer thresholds
tests/                    analyzer tests
tests/fixtures/           recorded captures used by the tests
runs/                     local capture output (not committed)
```

## Adding the plugin to a project

Link the plugin into the project's `Plugins` folder, so the repo stays the single copy:

```powershell
New-Item -ItemType Junction -Path <Project>\Plugins\PerfPipeline -Target <this repo>\ue-plugin\PerfPipeline
```

Project plugins are enabled by default, so no `.uproject` change is needed. The plugin does nothing unless the game is started with `-PerfOut`.

## Packaging a build

```powershell
& 'D:\UE4\UE_5.7\Engine\Build\BatchFiles\RunUAT.bat' BuildCookRun `
  -project='D:\UE5\LoadedDiceFaceOff\LoadedDiceFaceOff.uproject' -platform=Win64 `
  -clientconfig=Development -build -cook -stage -pak -archive `
  -archivedirectory='D:\UE5\PerfBuilds\LoadedDiceFaceOff' -map=/Game/EvilLair -unattended
```

## Running a capture

Runs start straight in a gameplay map, standalone, with Steam off:

```powershell
LoadedDiceFaceOff.exe /Game/EvilLair -game -windowed -ResX=1920 -ResY=1080 `
  -nosteam -ini:Engine:[OnlineSubsystem]:DefaultPlatformService=Null `
  -trace=default,gpu -statnamedevents -tracefile=<run>\capture.utrace `
  -PerfOut=<run> -PerfQuitAfter=30 -log -abslog=<run>\game.log
```

| Argument | Effect |
| --- | --- |
| `-PerfOut=<folder>` | Writes `frames.csv` (one row per frame) and `run_info.txt` (what the build can record) |
| `-PerfQuitAfter=<seconds>` | Quits the game after that many seconds |

`frames.csv` columns: `frame, t_ms, frame_ms, game_ms, render_ms, rhi_ms, gpu_ms`. Thread and GPU times come from the engine's frame timing values, so they don't depend on the CSV profiler or stats. `frame_ms` is the engine delta time, which describes the previous frame; ingest pairs each frame's thread times with the next row's `frame_ms`. Rows are kept in memory and written when recording stops, so the recorder never writes to disk mid-capture.

## Exporting the trace

Unreal Insights runs headless and executes export commands listed in a text file:

```powershell
UnrealInsights.exe -OpenTraceFile="<run>\capture.utrace" -AutoQuit -NoUI `
  -ExecOnAnalysisCompleteCmd="@=<run>\insights_cmds.txt" -ABSLOG="<run>\insights.log"
```

Commands confirmed in 5.7, one per line in the file:

```
TimingInsights.ExportThreads <out>\threads.csv
TimingInsights.ExportTimers <out>\timers.csv
TimingInsights.ExportTimerStatistics <out>\timer_stats.csv -threads=GameThread,RenderThread*,RHIThread,GPU* -timers=*
TimingInsights.ExportTimingEvents <out>\events.csv -threads=GameThread -timers=* -startTime=30 -endTime=31
```

`-startTime` and `-endTime` are in seconds on the trace clock, which starts at process launch.

## Analyzing a run

```powershell
pip install -e .[test]
ueperf analyze <run> [--budget-ms 11.1] [--thresholds my_thresholds.toml]
```

It prints a summary, the sustained overruns and the hitches, and writes `analysis.json` to the run folder.

- **Over budget:** a frame whose duration, plus compositor GPU time when Varjo is on, exceeds the budget (default 11.1 ms).
- **Bound type:** the largest of game, render, RHI and GPU time on that frame, or `mixed` when the top two are within 10%.
- **Sustained overrun:** 45 or more frames from the first to the last over-budget frame, with no more than 5 in-budget frames in a row.
- **Hitch:** 1 to 3 consecutive over-budget frames, each more than twice the median of the previous 90 frames.

All thresholds are in `ueperf/thresholds.toml`.

## Findings so far

From 30-second idle captures of EvilLair:

- Median frame 7.56 ms, p95 8.21 ms, and 12 frames over 11.1 ms, most of them during startup. There are 2 hitches, at 17.5 ms and 29.2 ms.
- Both hitches match `FEngineLoop::Tick` events of the same length in the trace. The trace clock runs 3.10 s ahead of the plugin's `t_ms`, because it starts at process launch rather than at plugin startup.
- The first recorder flushed to disk every 120 frames and caused an 11 to 18 ms frame each time. It now buffers in memory.
- The trace has full CPU timers on every thread and GPU pass timers. A 30-second trace with `-statnamedevents` is about 120 MB, so traces stay out of git.
