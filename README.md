# UE5TestPipeline

Automated performance playtests for UE5 projects. The pipeline plays a packaged build by firing the player's own input actions, records frame timing and an Unreal Insights trace, and reports what caused each hitch and budget overrun and where the biggest gains are.

The first project under test is LoadedDiceFaceOff (UE 5.7).

## Status

Milestone 1 is done: a bare plugin records per-frame timing in a packaged Development build, the build records a full Insights trace, and headless Insights exports it to CSV.

| Milestone | State |
| --- | --- |
| 1. Capture spike | Done |
| 2. Frame analysis | Not started |
| 3. Cause finding and gains ranking | Not started |
| 4. Single-run report | Not started |
| 5. Lifecycle markers and capture control | Not started |
| 6. Input driver | Not started |
| 7. Onboarding, launcher and supervision | Not started |
| 8. Results store | Not started |

## Requirements

- UE 5.7 installed from the Epic launcher. A custom-built engine is not supported.
- Visual Studio 2022 with the UE C++ workloads.
- Python 3.11 or newer.

## Build configuration

Captures use packaged **Development** builds. The launcher engine can't build Shipping with trace, because that needs a unique build environment, and it ships no precompiled Test libraries. Development builds carry some overhead that Shipping doesn't, so absolute frame times read slightly high, but the ranking of causes holds.

## Repo layout

```
ue-plugin/PerfPipeline/   UE plugin: frame recorder and capture control
tests/fixtures/           recorded captures used by the analyzer tests
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
  -PerfOut=<run> -PerfQuitAfter=60 -log -abslog=<run>\game.log
```

| Argument | Effect |
| --- | --- |
| `-PerfOut=<folder>` | Writes `frames.csv` (one row per frame) and `run_info.txt` (what the build can record) |
| `-PerfQuitAfter=<seconds>` | Quits the game after that many seconds |

`frames.csv` columns: `frame, t_ms, frame_ms, game_ms, render_ms, rhi_ms, gpu_ms`. Thread and GPU times come from the engine's frame timing values, so they don't depend on the CSV profiler or stats.

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

## Milestone 1 findings

From a 60-second idle capture of EvilLair (fixture `tests/fixtures/spike1`):

- 8,053 frames, median 7.34 ms, p95 7.74 ms, and 64 frames over 11.1 ms. The worst frame took 66 ms.
- The trace has full CPU timers on every thread and GPU pass timers for 4,994 timers in all. A 60-second trace with `-statnamedevents` is about 250 MB, so traces stay out of git.
- The plugin's `t_ms` starts at module startup, and the trace clock starts at process launch. The analyzer has to line them up using the `run_start` bookmark.
