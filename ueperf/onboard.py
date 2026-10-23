"""Drafts a game profile from a project's files, as the first onboarding step.

The draft is a starting point for the Claude agent doing the onboarding: it
names the maps, the launch arguments the project's plugins call for, the input
actions found in Content and code, and candidate lifecycle hooks, with comments
saying which still need a decision. The agent then reads the source to settle
those, runs a short trial and adjusts the profile until the run reaches
gameplay and fires most actions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Maps with these words in their name are menus, lobbies or test levels, never gameplay maps.
NON_GAMEPLAY_MAP = re.compile(r"menu|lobby|login|frontend|entry|startup|loading|splash|transition|test|empty",
                              re.IGNORECASE)
# Action names that usually leave gameplay or need online services.
RISKY_ACTION = re.compile(r"voice|chat|mute|menu|pause|quit|exit|escape|screenshot|debug|console", re.IGNORECASE)
INPUT_ACTION_MARKER = b"/Script/EnhancedInput"


def _ini_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", path.read_text(encoding="utf-8", errors="replace"), re.M)
    return m.group(1) if m else None


def find_maps(project: Path) -> tuple[list[str], str | None]:
    """Game paths of the project's maps, gameplay candidates first, and its default game map."""
    content = project.parent / "Content"
    maps = []
    for umap in sorted(content.rglob("*.umap")):
        rel = umap.relative_to(content).with_suffix("")
        if any(part.startswith("__") or part.lower() in ("developers", "collections") for part in rel.parts):
            continue
        maps.append("/Game/" + rel.as_posix())
    default = _ini_value(project.parent / "Config" / "DefaultEngine.ini", "GameDefaultMap")
    if default:
        default = default.split(".")[0]
    gameplay = [m for m in maps if not NON_GAMEPLAY_MAP.search(m.rsplit("/", 1)[-1]) and m != default]
    return gameplay + [m for m in maps if m not in gameplay], default


def find_input_actions(project: Path) -> list[str]:
    """Names of Enhanced Input action assets in Content, by their file name."""
    names = []
    for asset in (project.parent / "Content").rglob("*.uasset"):
        if asset.stat().st_size > 64 * 1024:
            continue  # action assets are a few KB; this skips textures and meshes cheaply
        data = asset.read_bytes()
        if INPUT_ACTION_MARKER in data and b"InputAction\x00" in data and b"InputMappingContext\x00" not in data:
            names.append(asset.stem)
    return sorted(set(names))


def find_poll_candidates(project: Path) -> list[str]:
    """Reflected bool functions and properties on game modes and game states, for match start and end."""
    out = []
    for header in (project.parent / "Source").rglob("*.h"):
        text = header.read_text(encoding="utf-8", errors="replace")
        owner = "GameMode" if re.search(r"class\s+(?:\w+_API\s+)?\w+\s*:\s*public\s+AGameMode", text) else (
            "GameState" if re.search(r"class\s+(?:\w+_API\s+)?\w+\s*:\s*public\s+AGameState", text) else None)
        if not owner:
            continue
        for m in re.finditer(r"UFUNCTION\([^)]*\)\s*(?:virtual\s+)?bool\s+(\w+)\s*\(\s*\)\s*(?:const)?", text):
            out.append(f"{owner}.{m.group(1)}  ({header.name})")
        for m in re.finditer(r"UPROPERTY\([^)]*\)\s*(?:uint8\s+|bool\s+)(b\w+)", text):
            out.append(f"{owner}.{m.group(1)}  ({header.name})")
    return out


def draft_profile(project: Path) -> str:
    project = project.resolve()
    uproject = json.loads(project.read_text(encoding="utf-8"))
    plugins = {p["Name"] for p in uproject.get("Plugins", []) if p.get("Enabled")}
    maps, default_map = find_maps(project)
    actions = find_input_actions(project)
    risky = [a for a in actions if RISKY_ACTION.search(a)]
    polls = find_poll_candidates(project)
    name = project.stem

    args = []
    if any(p.startswith("OnlineSubsystemSteam") for p in plugins):
        args = ["-nosteam", "-ini:Engine:[OnlineSubsystem]:DefaultPlatformService=Null"]

    lines = [
        f"# Game profile for {name}, drafted by `ueperf onboard`. Settle every TODO, then run a",
        "# short trial (`ueperf run <profile> --runs 1 --play-s 30`) and check the screenshots and coverage.",
        "",
        f'project = "{project.as_posix()}"',
        "version = 1",
        f"maps = [{json.dumps(maps[0]) if maps else ''}]"
        + "   # TODO: the gameplay map to test; never a menu or lobby",
        f"# all maps: {', '.join(maps[:20])}" + (" ..." if len(maps) > 20 else ""),
        f"# GameDefaultMap is {default_map}" if default_map else "# no GameDefaultMap set",
        f"args = {json.dumps(args)}" + ("   # standalone, without Steam" if args else ""),
        "budget_ms = 11.1",
        "runs = 3",
        "play_s = 300",
        f'build = "{(project.parent.parent / "PerfBuilds" / name / "Windows" / f"{name}.exe").as_posix()}"',
        "resolution = [1920, 1080]",
        "screenshot_every_s = 30",
        '# quit_on = "match_end"   # TODO: a lifecycle marker that ends the run early, if the game has one',
        "",
        "# Actions are found at runtime from the player's mapping contexts; this list is what Content holds:",
        f"# {', '.join(actions) if actions else 'no Enhanced Input action assets found'}",
        "[input]",
        'start_on = "pawn_possessed"',
        "start_delay_s = 1.0",
        f"exclude = {json.dumps(sorted(set(risky)))}"
        + "   # TODO: confirm; these look like menus, voice or debug",
        "voices = 2",
        "",
        "# TODO: game-specific lifecycle events. Candidates for poll on the game mode or game state:",
        *[f"#   {p}" for p in polls[:15]],
        "# [[lifecycle]]",
        '# name = "match_end"',
        '# poll = "GameMode.IsMatchEnded"',
        "",
        "[phases]",
        'map_load_start = "loading"',
        'map_load_end = "in_map"',
        'match_start = "match"',
        'match_end = "post_match"',
    ]
    return "\n".join(lines) + "\n"
