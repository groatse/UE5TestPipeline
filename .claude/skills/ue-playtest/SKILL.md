---
name: ue-playtest
description: Run and supervise an automatic performance playtest of a UE5 project with this repo's pipeline (onboard a new project, launch runs with ueperf run, watch screenshots, recover stuck runs, read the report). Use whenever asked to playtest, capture, profile or perf-test a UE5 game here.
---

# Automatic UE5 playtest

The plugin plays the game from inside the process by firing the player's own bound input actions (Enhanced Input injection, or key events for legacy input). You supervise from outside: the launcher takes a screenshot of the game window every `screenshot_every_s` seconds and prints `ATTENTION` lines when the run looks stuck. You look at screenshots, step in with `ueperf send` when needed, and read the analysis when the run ends. Runs are not replays: every run picks actions at random with the profile's weights, and favours actions not fired yet.

Commands run from the repo root with the venv: `.venv\Scripts\ueperf.exe`.

## 1. Onboard a project (once)

1. The project needs the plugin: junction `ue-plugin\PerfPipeline` into `<Project>\Plugins\PerfPipeline` (README, "Adding the plugin"), then package a Development build (README, "Packaging a build").
2. `ueperf onboard <Project>.uproject` drafts `profiles/<Project>.toml`. Settle every TODO by reading the project:
   - `maps`: a gameplay map that can be opened directly. Never a menu or lobby. Check the game mode starts play by itself (a timer, `BeginPlay`, `PostLogin`); if it needs a menu click, the agent has to do it with `ueperf send click` and you should say so.
   - `args`: `-nosteam` and the Null platform service when Steam or EOS is enabled.
   - `[input] exclude`: actions that open menus, pause, quit, use voice or debug. `Escape`-mapped actions are excluded by default, as are names with Pause, Menu, Quit or Exit.
   - `[input] start_on`: the marker after which input does something, usually `pawn_possessed`.
   - `[[lifecycle]]`: match start and end as `poll`, `spawn`, `log` or `scope` (README, "Lifecycle markers"). Drop events nothing can detect.
3. Trial run: `ueperf run profiles\<Project>.toml --runs 1 --play-s 30`. Check the screenshots show gameplay reacting to input, and that the coverage table fires all allowed actions. Adjust the profile and repeat until it does.

## 2. Run a playtest

Start `ueperf run profiles\<Project>.toml` in the background (each run takes the play time plus loading, then a few minutes of trace analysis). While it runs, every 30 s or whenever it prints `ATTENTION`:

1. `ueperf status` prints the live state (`driver` waiting, playing or paused, `pawn`, `cursor`, `paused_game`, actions fired) and the latest screenshot path.
2. Read the latest screenshot.
3. If gameplay is going on, do nothing. Otherwise recover:

| What you see | Send |
| --- | --- |
| A menu, dialog or "press any key" | `ueperf send click 0.5 0.62` (fractions of the window) or `ueperf send key Enter` |
| Pause menu | `ueperf send key Escape` |
| Dead, a respawn prompt | the prompt's key, such as `ueperf send key SpaceBar`, or `ueperf send action <Name>` |
| Driver stuck in `waiting` | check the start marker fired in `game.log`; `ueperf send resume` starts play now |
| Nothing recovers it | `ueperf send quit` ends the run, and say why |

Other commands: `pause`, `type <text>`, `exec <console command>`. Every command is logged in `inputs.csv` and listed in the report, so a recovery is never mistaken for gameplay.

## 3. Read the result

`ueperf run` analyzes each run and writes `runs\<profile>-<time>\report.html`. Check, and report to the user:

- `run.json` `ended`: `play_time` or `quit marker` is a normal end; `agent`, `time up` or `timeout` means the run was cut short.
- Input coverage: every allowed action fired. An action that never fired is either not bound in that state or needs a profile change.
- Each hitch lists the player input in the second before it, beside its hot spots. Report hitches as call paths and sources, never as a ranking.

Traces are about 120 MB per 30 s. Delete `capture.utrace` from runs you no longer need.
