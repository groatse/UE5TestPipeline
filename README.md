# UE5TestPipeline

Automated performance playtests for UE5 projects. The pipeline plays a packaged build by firing the player's own input actions, records frame timing and an Unreal Insights trace, and reports what caused each hitch and budget overrun and where the biggest gains are.

The first project under test is LoadedDiceFaceOff (UE 5.7).

## Status

Milestones 1 to 5 are done: the plugin records per-frame timing and lifecycle markers in a packaged Development build alongside a full Insights trace, and `ueperf analyze` finds sustained overruns and hitches, explains each as call paths from the trace, places it in its lifecycle phase, and ranks the gains.

| Milestone | State |
| --- | --- |
| 1. Capture spike | Done |
| 2. Frame analysis | Done |
| 3. Cause finding and gains ranking | Done |
| 4. Single-run report | Done |
| 5. Lifecycle markers and capture control | Done |
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
ue-plugin/PerfPipeline/   UE plugin: frame recorder, lifecycle markers, capture control
profiles/                 game profiles (run settings, lifecycle events, phases)
ueperf/                   Python package: ingest, analyzer, report, command line
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
  -PerfOut=<run> -PerfQuitAfter=30 -PerfEvents=<run>\events.json -PerfQuitOn=match_end `
  -log -abslog=<run>\game.log
```

| Argument | Effect |
| --- | --- |
| `-PerfOut=<folder>` | Writes `frames.csv` (one row per frame), `markers.csv` (lifecycle markers) and `run_info.txt` (what the build can record) |
| `-PerfQuitAfter=<seconds>` | Quits the game after that many seconds |
| `-PerfEvents=<file>` | JSON list of game-specific lifecycle events; `ueperf events <profile> <file>` writes it from a profile |
| `-PerfQuitOn=<marker>` | Quits the game when that lifecycle marker fires |

## Lifecycle markers

Markers are predictable lifecycle events, never gameplay segments. They label phases such as loading and the match, and give each hitch its context ("0.02 s after `countdown_start`"). None of them needs game code. Each marker goes to `markers.csv` and into the trace as an Insights bookmark (`PerfMarker <name> <detail>`).

- **Engine events, always on:** `run_start`, `map_load_start`, `map_load_end`, `player_login`, `pawn_possessed` (with the pawn class), `travel`, `match_state` (games using `AGameMode`), `run_end`.
- **Game events, from the profile's `[[lifecycle]]` entries:**
  - `poll = "GameMode.IsMatchEnded"`: a bool property or parameterless bool function on `GameMode`, `GameState`, `PlayerController` or `Pawn`, read through reflection every 0.1 s; fires when it turns true.
  - `spawn = "BP_DiceActor_C"`: an actor of that class, or with it in its class chain, is spawned.
  - `log = "GAME SUMMARY"`: a log line contains the text.
  - `scope = "Client_OnGameStarted"`: a trace scope, such as a UFunction or RPC. These are found by the analyzer in the trace after the run, for moments the game exposes to nothing reflected.
- **Phases:** the profile's `[phases]` maps markers to the phase they start. Frames before the first one are `startup`. Startup and loading frames stay out of baselines and the gains ranking.

For LoadedDiceFaceOff (`profiles/LoadedDiceFaceOff.toml`): `countdown_start` is the dice spawn in `AGameModeLoadedDice::StartGame`, `match_start` is the `Client_OnGameStarted` RPC from `LaunchMatch`, and `match_end` polls the game mode's `IsMatchEnded`.

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

### Cause finding

When the run folder has `capture.utrace`, `analyze` also reads the trace through headless Unreal Insights (found through the launcher install of UE 5.7, `--insights` or `UEPERF_INSIGHTS`). Pass `--profile <profile>` (or `--project <folder>`) so scopes in game code and Blueprint assets resolve, lifecycle scopes are found and phases apply. Use `--no-trace` to skip it.

```powershell
ueperf analyze runs\m5a --profile profiles\LoadedDiceFaceOff.toml
```

1. **Alignment:** every game thread `FEngineLoop::Tick` is exported, and recorded frames are matched to ticks of the same length, which gives each frame its interval on the trace clock.
2. **Export:** timing events are exported only for over-budget frames and 30 in-budget baseline frames, on the game, render and RHI threads, the GPU queues and the task graph workers. A full 30-second game thread export is close to 500 MB, so whole-run exports are avoided. The game thread counts a frame's own tick; render, RHI, GPU and workers count that tick and the next one, because they work on a frame during the following tick.
3. **Call paths:** events are rebuilt into call paths, the chain of scopes from a thread's top-level scope down to each scope, with inclusive and own (exclusive) time. Each path is compared with its median on the baseline frames.
4. **Hot spots:** paths whose own time rose are where the extra time was actually spent. A hot spot with a large own rise and nothing instrumented below it is the place to add a scope or start reading code. When the bound thread's rise is waiting (`WaitForTasks` and similar), worker paths that rose are hot spots too, scaled so they never claim more than the wait.
5. **What is shown:** each hot spot leads with its source (file:line and function), then the scope, and a "called from" dropdown with the caller chain. Framework scopes every frame passes through (engine loop, world tick, Slate tick, scene render, task dispatch; `trim` in `thresholds.toml`) are left out of that chain. Every thread is searched; the GPU only when the frame log's GPU time rose, because GPU scopes read over a long CPU frame always look larger. The full call tree is shown only when the hot spots explain less than half of the stall, which is when time is spread over many small scopes.
6. **What each scope is:** names resolve through Insights' own timer sources, a source index of engine and project code (cycle stats, quick scopes, CPU profiler scopes, named events, RDG passes, with the enclosing function), and the project's `Content` assets. Scopes are labelled C++, Blueprint function or event (with the owning Blueprint and asset path), object (Blueprint class or widget animation), UObject, GPU pass or wait. The engine index takes about 45 s on first use and is cached in `%LOCALAPPDATA%\ueperf`; `--reindex` rebuilds it.
7. **Gains:** hot-spot rises on each gameplay over-budget frame, scaled so a frame never gives out more than its excess, are summed per call path and ranked. Startup and loading frames, by lifecycle phase, are left out; runs without markers fall back to skipping the first 2 seconds.

The plugin turns on verbose named events, so the trace names more per-object work (component registration, widget animation instances).

### Loading time

The game counts as **playable** from the first frame that starts a 2 s stretch with at most 1% of frames over budget and none over 1.5x the budget (`[playable]` in `thresholds.toml`). The report gives launch to playable (the plugin records how long the process ran before recording began), map load (`map_load_start` to `map_load_end`) and map loaded to playable. Frames between the map load and playable are the `warmup` phase: they show in the chart and the hitch list, but are left out of baselines and the gains ranking, so level-load leftovers such as texture creation, PSO compiles and the RHI delete queue don't crowd out what can be tuned in gameplay.
### Report

`analyze` also writes `report.html` to the run folder; `ueperf report <run>` rewrites it from an existing `analysis.json`. It is one self-contained file that opens offline and follows the system light or dark theme:

- summary tiles (median, p95, p99, worst frame, share over budget, hitch and overrun counts)
- frame time against the budget line, with hitches, sustained overruns and the startup window marked
- game, render, RHI and GPU time per frame
- hovering either chart shows the frame's numbers and its approximate trace time
- each hitch and overrun with its hot spots (own-time rise, source location, full call path) and a call tree
- hitches grouped by top hot spot, the gains ranking by call path, and the run info

## Findings so far

From 30-second idle captures of EvilLair:

- Median frame 7.56 ms, p95 8.21 ms, and 12 frames over 11.1 ms, most of them during startup. There are 2 hitches, at 17.5 ms and 29.2 ms.
- The 29 to 38 ms game thread hitch about 10.6 s in is the first play of a HUD animation. A timer on `BP_GameModeLoadedDice_C` runs `Client_OnCountdownTick`, then `OnCountdownTick` in `BP_ControllerLoadedDice`, then `SetCounterVisibility` and `PlayAnimationForward` in `W_GameHUD`, then `Widget::PlayAnimation` (`UserWidget.cpp:844`). On the next Slate tick the MovieScene runner's `Spawn Phase` (`MovieSceneEntitySystemRunner.cpp:1080`) takes about 12 ms to set up the `CounterFadeIn` animation's entities. In the same frame, spawning the pawn registers a `SpringArm` component (+2.3 ms).
- The 17 to 24 ms render thread hitch is the render thread waiting on workers. They are compiling a pipeline state at runtime (`Create time`, `WindowsD3D12PipelineState.cpp:758`, under `PSOPrecache: Precached` for `GPUScene.UploadDynamicPrimitiveShaderDataForView`) and processing the RHI delete queue.
- Both hitches match `FEngineLoop::Tick` events of the same length in the trace. The trace clock runs 3.10 s ahead of the plugin's `t_ms`, because it starts at process launch rather than at plugin startup.
- The first recorder flushed to disk every 120 frames and caused an 11 to 18 ms frame each time. It now buffers in memory.
- The trace has full CPU timers on every thread and GPU pass timers. A 30-second trace with `-statnamedevents` is about 120 MB, so traces stay out of git.
