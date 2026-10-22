"""Runs Unreal Insights headless to export data from a .utrace file."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

LAUNCHER_MANIFEST = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat"
INSIGHTS_RELATIVE = Path("Engine") / "Binaries" / "Win64" / "UnrealInsights.exe"


class InsightsError(RuntimeError):
    pass


def find_insights(explicit: Path | None = None, engine_version: str = "5.7") -> Path:
    """Locate UnrealInsights.exe.

    Order: `explicit`, the UEPERF_INSIGHTS environment variable, then the
    launcher install of UE `engine_version`.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("UEPERF_INSIGHTS"):
        candidates.append(Path(os.environ["UEPERF_INSIGHTS"]))
    if LAUNCHER_MANIFEST.exists():
        manifest = json.loads(LAUNCHER_MANIFEST.read_text(encoding="utf-8"))
        for item in manifest.get("InstallationList", []):
            if item.get("AppName") == f"UE_{engine_version}":
                candidates.append(Path(item["InstallLocation"]) / INSIGHTS_RELATIVE)

    for path in candidates:
        if path.exists():
            return path
    raise InsightsError("UnrealInsights.exe not found; pass --insights or set UEPERF_INSIGHTS")


def run_commands(insights: Path, trace: Path, commands: list[str], workdir: Path, timeout_s: float = 900) -> None:
    """Analyze `trace` once and run each TimingInsights export command on it."""
    workdir.mkdir(parents=True, exist_ok=True)
    cmd_file = workdir / "insights_cmds.txt"
    cmd_file.write_text("\n".join(commands) + "\n", encoding="ascii")
    log = workdir / "insights.log"
    args = [
        str(insights),
        f"-OpenTraceFile={trace}",
        f"-ABSLOG={log}",
        "-AutoQuit",
        "-NoUI",
        f"-ExecOnAnalysisCompleteCmd=@={cmd_file}",
    ]
    try:
        subprocess.run(args, check=True, timeout=timeout_s)
    except subprocess.CalledProcessError as e:
        raise InsightsError(f"Unreal Insights failed with exit code {e.returncode}; see {log}") from e
    except subprocess.TimeoutExpired as e:
        raise InsightsError(f"Unreal Insights did not finish in {timeout_s:.0f} s; see {log}") from e
