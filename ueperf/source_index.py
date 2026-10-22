"""Maps trace scope names to the source lines that emit them.

Trace scopes carry a name but usually no source location: cycle stats, quick
scopes, named events and RDG pass names are all just strings in the trace.
This index scans engine and project source for the macros that create those
scopes and records where each name is declared and used, with the enclosing
function, so a scope in a hitch's call path can be followed to code.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

INDEX_VERSION = 1
SOURCE_EXTENSIONS = (".cpp", ".h", ".inl")

# Name declarations: the display name a stat id shows up as in the trace.
DECLARE_RE = re.compile(
    r'\b(?:DECLARE_CYCLE_STAT(?:_EXTERN|_WITH_FLAGS)?|DECLARE_DWORD_COUNTER_STAT|DECLARE_FLOAT_COUNTER_STAT)'
    r'\s*\(\s*TEXT\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)\s*,\s*(\w+)')
# Scopes that name themselves where they are used.
USE_NAMED_RES = [
    re.compile(r'\bDECLARE_SCOPE_CYCLE_COUNTER\s*\(\s*TEXT\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)\s*,\s*(\w+)'),
    re.compile(r'\bTRACE_CPUPROFILER_EVENT_SCOPE_(?:STR|TEXT)\s*\(\s*(?:TEXT\s*\(\s*)?"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\bSCOPED_NAMED_EVENT_(?:TEXT|TCHAR|STR)\s*\(\s*(?:TEXT\s*\(\s*)?"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\bRDG_EVENT_(?:SCOPE|SCOPE_STAT|SCOPE_CONDITIONAL)\s*\([^"]*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\bRDG_EVENT_NAME\s*\(\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'\bSCOPED_DRAW_EVENT(?:F)?\s*\(\s*\w+\s*,\s*(\w+)'),
]
# Scopes that use an identifier as their trace name.
USE_IDENT_RES = [
    re.compile(r'\bTRACE_CPUPROFILER_EVENT_SCOPE\s*\(\s*([A-Za-z_][\w:]*)\s*\)'),
    re.compile(r'\bSCOPED_NAMED_EVENT\s*\(\s*([A-Za-z_]\w*)\s*,'),
    re.compile(r'\bQUICK_SCOPE_CYCLE_COUNTER\s*\(\s*(\w+)\s*\)'),
]
# Scopes that refer to a stat declared elsewhere.
USE_STAT_RES = [
    re.compile(r'\bSCOPE_CYCLE_COUNTER\s*\(\s*(\w+)\s*\)'),
    re.compile(r'\bCONDITIONAL_SCOPE_CYCLE_COUNTER\s*\(\s*(\w+)\s*,'),
    re.compile(r'\bSCOPE_SECONDS_ACCUMULATOR\s*\(\s*(\w+)\s*\)'),
]
KEYWORDS = (b"CYCLE", b"TRACE_CPUPROFILER", b"NAMED_EVENT", b"RDG_EVENT", b"DRAW_EVENT")

# A function definition line: starts in column 0, has Class::Method( or Name(.
FUNC_RE = re.compile(r'^(?!\s)(?!#)(?!if\b|for\b|while\b|switch\b|return\b)[\w:<>,\s\*&~]*?\b((?:\w+::)*~?\w+)\s*\(')


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    function: str

    def label(self) -> str:
        where = f"{self.file}:{self.line}"
        return f"{where} in {self.function}" if self.function else where


def _enclosing_function(lines: list[str], index: int) -> str:
    for i in range(index, max(-1, index - 400), -1):
        m = FUNC_RE.match(lines[i])
        if m and not lines[i].rstrip().endswith(";"):
            return m.group(1)
    return ""


def _scan_file(path: str) -> tuple[list, list, list]:
    """(declarations, named uses, stat uses) found in one file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return [], [], []
    if not any(k in data for k in KEYWORDS):
        return [], [], []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    decls, named, stats = [], [], []
    for n, line in enumerate(lines):
        if "CYCLE" not in line and "TRACE_" not in line and "EVENT" not in line and "COUNTER" not in line:
            continue
        for m in DECLARE_RE.finditer(line):
            decls.append((m.group(1), m.group(2), path, n + 1))
        for rx in USE_NAMED_RES:
            for m in rx.finditer(line):
                named.append((m.group(1), path, n + 1, _enclosing_function(lines, n)))
                if rx.groups >= 2:
                    decls.append((m.group(1), m.group(2), path, n + 1))
        for rx in USE_IDENT_RES:
            for m in rx.finditer(line):
                named.append((m.group(1), path, n + 1, _enclosing_function(lines, n)))
        for rx in USE_STAT_RES:
            for m in rx.finditer(line):
                stats.append((m.group(1), path, n + 1, _enclosing_function(lines, n)))
    return decls, named, stats


def _source_files(roots: list[Path]) -> list[str]:
    files = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip build output and third-party code, which has no UE scopes worth naming.
            dirnames[:] = [d for d in dirnames if d not in ("Intermediate", "Binaries", "ThirdParty", "Saved", "DerivedDataCache", ".git")]
            files.extend(os.path.join(dirpath, f) for f in filenames if f.endswith(SOURCE_EXTENSIONS))
    return files


class SourceIndex:
    def __init__(self, names: dict[str, list[Site]], declared: dict[str, Site]):
        self.names = names
        self.declared = declared

    def sites(self, scope_name: str) -> list[Site]:
        """Where a trace scope with this name is used, most specific first."""
        if scope_name in self.names:
            return self.names[scope_name]
        # Format-string scopes such as "BasePass %s" export with the format intact,
        # and numbered object instances export with their suffix.
        stripped = re.sub(r"_\d+$", "", scope_name)
        return self.names.get(stripped, [])

    def declaration(self, scope_name: str) -> Site | None:
        return self.declared.get(scope_name)

    @classmethod
    def build(cls, roots: list[Path], workers: int | None = None) -> "SourceIndex":
        files = _source_files(roots)
        stat_names: dict[str, str] = {}
        declared: dict[str, Site] = {}
        names: dict[str, list[Site]] = defaultdict(list)
        stat_uses: list[tuple] = []
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for decls, named, stats in pool.map(_scan_file, files, chunksize=64):
                for display, stat_id, path, line in decls:
                    stat_names.setdefault(stat_id, display)
                    declared.setdefault(display, Site(path, line, ""))
                for name, path, line, func in named:
                    names[name].append(Site(path, line, func))
                stat_uses.extend(stats)
        for stat_id, path, line, func in stat_uses:
            display = stat_names.get(stat_id, stat_id)
            names[display].append(Site(path, line, func))
        return cls(dict(names), declared)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "names": {k: [asdict(s) for s in v] for k, v in self.names.items()},
            "declared": {k: asdict(v) for k, v in self.declared.items()},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SourceIndex | None":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("version") != INDEX_VERSION:
            return None
        names = {k: [Site(**s) for s in v] for k, v in payload["names"].items()}
        declared = {k: Site(**v) for k, v in payload["declared"].items()}
        return cls(names, declared)

    def merged(self, other: "SourceIndex") -> "SourceIndex":
        names = {k: list(v) for k, v in self.names.items()}
        for k, v in other.names.items():
            names[k] = v + names.get(k, [])  # project sites first
        declared = dict(self.declared)
        declared.update(other.declared)
        return SourceIndex(names, declared)


def engine_roots(engine: Path) -> list[Path]:
    return [engine / "Engine" / "Source" / "Runtime", engine / "Engine" / "Plugins"]


def project_roots(project_dir: Path) -> list[Path]:
    return [project_dir / "Source", project_dir / "Plugins"]


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "ueperf"


def load_or_build(engine: Path | None, project_dir: Path | None, rebuild: bool = False) -> SourceIndex:
    """Engine index cached per engine install, merged with a fresh project index."""
    index = SourceIndex({}, {})
    if engine:
        cache = cache_dir() / f"source_index_{re.sub(r'[^A-Za-z0-9]+', '_', str(engine))}.json"
        engine_index = None if rebuild else SourceIndex.load(cache)
        if engine_index is None:
            print(f"Indexing engine source in {engine} (cached after the first run) ...")
            engine_index = SourceIndex.build(engine_roots(engine))
            engine_index.save(cache)
        index = engine_index
    if project_dir:
        index = index.merged(SourceIndex.build(project_roots(project_dir)))
    return index
