# UE5TestPipeline

Automated performance playtests for UE5 projects. The pipeline plays a packaged build by firing the player's own input actions, records frame timing and an Unreal Insights trace, and reports exactly what caused each hitch and budget overrun: the source, the call path and the game class or Blueprint behind it.

The first project under test is LoadedDiceFaceOff (UE 5.7).

## Status

Milestones 1 to 7 are done. `ueperf run <profile>` starts a packaged Development build, the plugin plays it by firing the player's own bound input actions while it records per-frame timing, lifecycle markers and a full Insights trace, the launcher screenshots the game for the supervising Claude agent, and `ueperf analyze` finds sustained overruns and hitches, explains each from the trace down to the source and the Blueprint or object behind it, and lists the player input around it and the run's input coverage.

| Milestone | State |
| --- | --- |
| 1. Capture spike | Done |
| 2. Frame analysis | Done |
| 3. Cause finding | Done |
| 4. Single-run report | Done |
| 5. Lifecycle markers and capture control | Done |
| 6. Input driver | Done |
| 7. Onboarding, launcher and supervision | Done |
| 8. Results store | Not started |

## Requirements

- UE 5.7 installed from the Epic launcher. A custom-built engine is not supported.
- Visual Studio 2022 with the UE C++ workloads.
- Python 3.11 or newer, with pytest for the tests.

## Build configuration

Captures use packaged **Development** builds. The launcher engine can't build Shipping with trace, because that needs a unique build environment, and it ships no precompiled Test libraries. Development builds carry some overhead that Shipping doesn't, so absolute frame times read slightly high, but the ranking of causes holds.

## Repo layout

```
ue-plugin/PerfPipeline/   UE plugin: input driver, frame recorder, lifecycle markers, capture control
profiles/                 game profiles (run settings, input rules, lifecycle events, phases)
ueperf/                   Python package: launcher, onboarding, ingest, analyzer, report, command line
  thresholds.toml         default analyzer thresholds
.claude/skills/ue-playtest/  how the Claude agent onboards a project, runs and supervises playtests
tests/                    analyzer and pipeline tests
tests/fixtures/           recorded captures used by the tests
runs/                     local capture output (not committed)
```

## Adding the plugin to a project

Link the plugin into the project's `Plugins` folder, so the repo stays the single copy:

```powershell
New-Item -ItemType Junction -Path <Project>\Plugins\PerfPipeline -Target <this repo>\ue-plugin\PerfPipeline
```

Project plugins are enabled by default, so no `.uproject` change is needed. The plugin does nothing unless the game is started with `-PerfOut`. It depends on the engine's Enhanced Input plugin, which UE5 enables by default.

## Packaging a build

```powershell
& 'D:\UE4\UE_5.7\Engine\Build\BatchFiles\RunUAT.bat' BuildCookRun `
  -project='D:\UE5\LoadedDiceFaceOff\LoadedDiceFaceOff.uproject' -platform=Win64 `
  -clientconfig=Development -build -cook -stage -pak -archive `
  -archivedirectory='D:\UE5\PerfBuilds\LoadedDiceFaceOff' -map=/Game/EvilLair -unattended
```

After a change to the plugin's code only, rebuilding the game target and copying the binary into the packaged build is enough, since the plugin has no content:

```powershell
& 'D:\UE4\UE_5.7\Engine\Build\BatchFiles\Build.bat' LoadedDiceFaceOff Win64 Development -Project='D:\UE5\LoadedDiceFaceOff\LoadedDiceFaceOff.uproject'
Copy-Item D:\UE5\LoadedDiceFaceOff\Binaries\Win64\LoadedDiceFaceOff.exe, D:\UE5\LoadedDiceFaceOff\Binaries\Win64\LoadedDiceFaceOff.pdb D:\UE5\PerfBuilds\LoadedDiceFaceOff\Windows\LoadedDiceFaceOff\Binaries\Win64\
```

## Running a playtest

Playtests always run through a Claude agent, which follows `.claude/skills/ue-playtest/SKILL.md`: it onboards the project once, starts the runs and supervises them by screenshot. An engineer can run the same commands.

```powershell
ueperf run profiles\LoadedDiceFaceOff.toml [--runs 3] [--play-s 300] [--map /Game/EvilLair] [--no-trace] [--no-analyze]
```

Each run gets a folder `runs\<profile>-<date>-<time>` (and `runs\latest.txt` points to it). The launcher:

1. collects machine data into `machine.json`: CPU, cores, RAM, GPUs with driver, VRAM and refresh rate, Windows build, power plan and machine name;
2. writes the plugin's `events.json` and `play.json` from the profile, and an empty `commands.txt`;
3. starts the real game binary behind the packaged build's launcher stub, straight into the profile's first map, standalone, with every path absolute;
4. screenshots the game window every `screenshot_every_s` seconds into `screens\`, reads the plugin's `status.json` every second, and prints a line starting with `ATTENTION` when a state lasts 10 s: no pawn, the mouse cursor showing (a menu is likely open), the game paused, no new frames, or no allowed actions bound (60 s for the driver still waiting to start);
5. waits for the plugin to end the run, or ends it after the play time plus 180 s;
6. writes `run.json` (profile, build and its time, map, play time, budget, command line, how the run ended, exit code, ATTENTION lines), then runs `ueperf analyze` on the folder.

The run ends with `play_time` once the driver has played `play_s` seconds, `quit marker` on the profile's `quit_on` marker, `agent` when the agent sent `quit`, or `time up` on the plugin's own limit (play time plus 150 s). Capture time is loading plus play time, so a 30-second run records about 40 s.

Screenshots use `PrintWindow` with `PW_RENDERFULLCONTENT`, which copies the window through the compositor, so the game does no readback. In a run with a screenshot every 1.5 s, the 442 frames within 150 ms of a screenshot had the same median (7.91 ms) and a lower p99 (10.74 ms against 11.11 ms) than the rest. A hitch within 250 ms of a screenshot says so in the report anyway.

### Watching a run

| Command | Effect |
| --- | --- |
| `ueperf status [<run>]` | live status: driver waiting, playing or paused, pawn, cursor, paused game, actions fired, latest screenshot |
| `ueperf send pause` / `resume` | stop or restart the input driver |
| `ueperf send key <Key> [hold_s]` | press a key through Slate, such as `Enter` or `Escape`, for menus and dialogs |
| `ueperf send click <x> <y>` | left click at a point of the game window, as fractions 0 to 1 |
| `ueperf send type <text>` | type text through Slate |
| `ueperf send action <Name> [hold_s]` | fire one input action |
| `ueperf send exec <console command>` | run a console command |
| `ueperf send quit` | end the run |

`send` appends the line to the run's `commands.txt`, which the plugin reads four times a second. Without `--run` it goes to the latest run. Every command is logged in `inputs.csv` and listed in the report.

## Input driver

The plugin plays the game from inside the process, so input reacts every frame and costs microseconds. Nothing in the game needs setup:

- **Finding actions:** once a second, every Enhanced Input action in the player's active mapping contexts and every action bound on the player controller's and pawn's input components, with its value type and keys. New pawns and contexts are picked up as they appear. Projects on the legacy input system get their action and axis mappings, fired as key events.
- **Playing:** `voices` actions at a time (default 2). Buttons get taps, double taps and holds; 1D axes get held directions and sine sweeps; 2D and 3D axes get held directions and circular sweeps, as per-frame deltas (`mouse_scale`) when mapped to a mouse axis. Each pick is random with the profile's weights, an action not fired yet counts three times, and gaps between gestures are 0.1 to 0.6 s. Runs are not exact repeats; the seed is recorded in `actions.csv`.
- **Leaving gameplay is avoided:** actions named like `*Pause*`, `*Menu*`, `*Quit*`, `*Exit*` or `*Escape*`, and actions mapped to `Escape`, are never fired unless the profile sets `exclude_defaults = false`.
- **Logging:** `inputs.csv` has one row per gesture (frame, time on the frame log's clock, action, gesture, value, duration) and per agent command; `actions.csv` lists every action found with its keys, where it was found, whether it was excluded, how often it fired and how long it was held.

The profile's `[input]` table sets the rules:

```toml
[input]
start_on = "pawn_possessed"       # marker after which input does something
start_delay_s = 1.0
exclude = ["*Voice*", "*Mute*"]   # action names, wildcards allowed
exclude_keys = []                 # actions mapped to these keys, on top of Escape
voices = 2
mouse_scale = 5.0
seed = 0                          # 0: a new seed every run
[input.weights]
Play_Look = 3
```

## Onboarding a project

`ueperf onboard <Project>.uproject` drafts `profiles\<Project>.toml` from the project's files: its maps with menus and lobbies sorted last, `-nosteam` and the Null platform service when Steam is enabled, the Enhanced Input action assets in Content with menu, voice and debug ones proposed for exclusion, and the reflected bool functions and properties on its game modes and game states as candidates for match start and end. Each decision left is marked TODO. The Claude agent settles them by reading the source, runs a 30-second trial, and adjusts the profile until the screenshots show gameplay and every allowed action fires.

## Running a capture by hand

Runs start straight in a gameplay map, standalone, with Steam off:

```powershell
LoadedDiceFaceOff.exe /Game/EvilLair -game -windowed -ResX=1920 -ResY=1080 `
  -nosteam -ini:Engine:[OnlineSubsystem]:DefaultPlatformService=Null `
  -trace=default,gpu -statnamedevents -tracefile=<run>\capture.utrace `
  -PerfOut=<run> -PerfQuitAfter=30 -PerfEvents=<run>\events.json -PerfQuitOn=match_end -PerfPlay=<run>\play.json `
  -log -abslog=<run>\game.log
```

| Argument | Effect |
| --- | --- |
| `-PerfOut=<folder>` | Writes `frames.csv` (one row per frame), `markers.csv` (lifecycle markers) and `run_info.txt` (what the build can record) |
| `-PerfQuitAfter=<seconds>` | Quits the game after that many seconds |
| `-PerfEvents=<file>` | JSON list of game-specific lifecycle events; `ueperf events <profile> <file>` writes it from a profile |
| `-PerfQuitOn=<marker>` | Quits the game when that lifecycle marker fires |
| `-PerfPlay=<file>` | Plays the game with the input driver; JSON written by `ueperf run` from the profile's `[input]`, plus `play_s` and the `commands` file. Adds `inputs.csv`, `actions.csv`, `status.json` and `run_end.txt` to the output folder |

## Lifecycle markers

Markers are predictable lifecycle events, never gameplay segments. They label phases such as loading and the match, and give each hitch its context ("0.02 s after `countdown_start`"). None of them needs game code. Each marker goes to `markers.csv` and into the trace as an Insights bookmark (`PerfMarker <name> <detail>`).

- **Engine events, always on:** `run_start`, `map_load_start`, `map_load_end`, `player_login`, `pawn_possessed` (with the pawn class), `travel`, `match_state` (games using `AGameMode`), `run_end`.
- **Game events, from the profile's `[[lifecycle]]` entries:**
  - `poll = "GameMode.IsMatchEnded"`: a bool property or parameterless bool function on `GameMode`, `GameState`, `PlayerController` or `Pawn`, read through reflection every 0.1 s; fires when it turns true.
  - `spawn = "BP_DiceActor_C"`: an actor of that class, or with it in its class chain, is spawned.
  - `log = "GAME SUMMARY"`: a log line contains the text.
  - `scope = "Client_OnGameStarted"`: a trace scope, such as a UFunction or RPC. These are found by the analyzer in the trace after the run, for moments the game exposes to nothing reflected.
- **Phases:** the profile's `[phases]` maps markers to the phase they start. Frames before the first one are `startup`. No hitches or overruns are recorded during startup, loading or warmup, and those frames stay out of baselines.

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
7. **Who is behind it:** when the hot spot is an engine system, the report names the game class, Blueprint or object behind it. **Called by** is the nearest Blueprint or object above it in its own call path. **Working on** is the object an engine system was processing, found under the same system scope in that frame (such as a widget animation instance). **Requested by** is the Blueprint that queued the work earlier in the frame, for systems listed in `[causes.requesters]` in `thresholds.toml` (MovieScene's `Runner Flush` is queued by `Widget::PlayAnimation`, for example).

The plugin turns on verbose named events, so the trace names more per-object work (component registration, widget animation instances).

### Loading time

The game counts as **playable** from the first frame that starts a 2 s stretch with at most 1% of frames over budget and none over 1.5x the budget (`[playable]` in `thresholds.toml`). The report gives launch to playable (the plugin records how long the process ran before recording began), map load (`map_load_start` to `map_load_end`) and map loaded to playable. Frames between the map load and playable are the `warmup` phase: they show in the chart, but no hitches or overruns are recorded there and they stay out of baselines, so level-load leftovers such as texture creation, PSO compiles and the RHI delete queue aren't reported as gameplay hitches.

### Report

`analyze` also writes `report.html` to the run folder; `ueperf report <run>` rewrites it from an existing `analysis.json`. It is one self-contained file that opens offline and follows the system light or dark theme:

- summary tiles (median, p95, p99, worst frame, share over budget, hitch and overrun counts)
- loading: launch to playable, launch to engine up, map load, and map loaded to playable
- frame time against the budget line, with hitches, sustained overruns, phases, markers and the playable point marked
- game, render, RHI and GPU time per frame
- hovering either chart shows the frame's numbers and its approximate trace time
- each hitch and overrun with its hot spots, each led by its source, with called by, working on and requested by, and the trimmed caller chain behind a dropdown
- the player input from a second before each hitch or overrun to its end, and whether a screenshot was taken next to it
- lifecycle phases and markers
- input coverage: every action found, its type, keys and where it was found, how often it fired and how long it was held, and any agent commands
- the run, its machine and the build's run info

## Findings so far

From 30-second playtests of EvilLair with the input driver (all 9 allowed actions fired in every run):

- A 37 ms hitch during the match, with the player smacking, looking and using items 2 and 3: a `BP_DiceItemActor` moving updates its overlaps, and its `PickupSphere` overlap query (`GeomOverlapMultiImp`, `SceneQuery.cpp:1098`, under `USceneComponent::UpdateOverlaps`) took 31.7 ms on the game thread. Idle captures never showed it.
- Launch to playable is 6.4 s, map load 0.33 s.

From 30-second idle captures of EvilLair:

- Median frame 7.56 ms, p95 8.21 ms, and 12 frames over 11.1 ms, most of them during startup. There are 2 hitches, at 17.5 ms and 29.2 ms.
- The 29 to 38 ms game thread hitch about 10.6 s in is the first play of a HUD animation. A timer on `BP_GameModeLoadedDice_C` runs `Client_OnCountdownTick`, then `OnCountdownTick` in `BP_ControllerLoadedDice`, then `SetCounterVisibility` and `PlayAnimationForward` in `W_GameHUD`, then `Widget::PlayAnimation` (`UserWidget.cpp:844`). On the next Slate tick the MovieScene runner's `Spawn Phase` (`MovieSceneEntitySystemRunner.cpp:1080`) takes about 12 ms to set up the `CounterFadeIn` animation's entities. In the same frame, spawning the pawn registers a `SpringArm` component (+2.3 ms).
- The 17 to 24 ms render thread hitch is the render thread waiting on workers. They are compiling a pipeline state at runtime (`Create time`, `WindowsD3D12PipelineState.cpp:758`, under `PSOPrecache: Precached` for `GPUScene.UploadDynamicPrimitiveShaderDataForView`) and processing the RHI delete queue.
- Both hitches match `FEngineLoop::Tick` events of the same length in the trace. The trace clock runs 3.10 s ahead of the plugin's `t_ms`, because it starts at process launch rather than at plugin startup.
- The first recorder flushed to disk every 120 frames and caused an 11 to 18 ms frame each time. It now buffers in memory.
- The trace has full CPU timers on every thread and GPU pass timers. A 30-second trace with `-statnamedevents` is about 120 MB, so traces stay out of git.
