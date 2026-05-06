from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

SERVER_NAME: Final = "tome-research"
SERVER_VERSION: Final = "0.1.0"
PROTOCOL_VERSION: Final = "2024-11-05"

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT: Final = REPO_ROOT.parent
DEFAULT_SOURCE_ROOTS: Final = (
    WORKSPACE_ROOT / ".tmp" / "t-engine4-tome-1.7.6",
    REPO_ROOT / "tools" / "t-engine4-master",
)
DEFAULT_INSTALL_ROOT: Final = Path(r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal")

TEXT_SUFFIXES: Final = {
    ".bat",
    ".c",
    ".cfg",
    ".cpp",
    ".css",
    ".frag",
    ".h",
    ".hpp",
    ".json",
    ".lua",
    ".luadoc",
    ".md",
    ".moon",
    ".py",
    ".sh",
    ".txt",
    ".vert",
    ".xml",
}
BINARY_SUFFIXES: Final = {
    ".dll",
    ".exe",
    ".fon",
    ".gif",
    ".jpg",
    ".mp3",
    ".ogg",
    ".png",
    ".so",
    ".teaa",
    ".team",
    ".teae",
    ".ttf",
    ".wav",
    ".zip",
}
SKIP_DIR_NAMES: Final = {".git", "__pycache__", "gfx", "music", "sound", "sounds", "video", "videos"}
MAX_TEXT_BYTES: Final = 1_500_000
MAX_RESULTS: Final = 200

SCOPES: Final = ("all", "engine", "module", "combat", "addons", "logs")

LOG_FILES: Final = {
    "te4": "te4_log.txt",
    "web": "te4_log_web.txt",
    "debug": "debug.log",
}

BATTLE_RESEARCH_QUERIES: Final = {
    "overview": (
        "attackTargetWith",
        "combatDamage",
        "combatAttack",
        "combatArmorHardiness",
        "combatGetResist",
        "combatGetDamageIncrease",
        "physicalCrit",
        "DamageType:get",
    ),
    "damage": (
        "attackTargetWith",
        "DamageType:get",
        "projector",
        "combatGetDamageIncrease",
        "resists_pen",
        "inc_damage",
    ),
    "hit": (
        "combatAttack",
        "combatDefense",
        "evasion",
        "checkHit",
        "target:checkHit",
    ),
    "armor": (
        "combatArmor",
        "combatArmorHardiness",
        "combatAPR",
        "armor",
        "hardiness",
    ),
    "crit": (
        "physicalCrit",
        "spellCrit",
        "mindCrit",
        "combat_critical_power",
        "crit_power",
    ),
    "talent": (
        "callbackOnMeleeAttack",
        "callbackOnMeleeHit",
        "weapon_mult",
        "newTalent",
        "requires_target",
    ),
    "practice": (
        "loadGame",
        "runAI",
        "game:onTurn",
        "set_addons",
        "game:registerDialog",
    ),
}

BATTLE_ANCHORS: Final = (
    (
        "Melee damage pipeline",
        "game/modules/tome/class/interface/Combat.lua",
        "attackTarget and attackTargetWith are the first source of truth for weapon hits.",
    ),
    (
        "Damage type projection",
        "game/engines/default/engine/DamageType.lua",
        "DamageType projector functions are where raw damage becomes typed damage.",
    ),
    (
        "Line/projectile delivery",
        "game/engines/default/engine/interface/ActorProject.lua",
        "Ranged, beam, cone, ball, and projectile talents flow through ActorProject.",
    ),
    (
        "Actor combat fields",
        "game/modules/tome/class/Actor.lua",
        "Actor init and temporary value rules explain resists, inc_damage, and combat fields.",
    ),
    (
        "Resources and life",
        "game/engines/default/engine/interface/ActorLife.lua",
        "Life/death hooks and damage reception live here.",
    ),
    (
        "Talent execution",
        "game/engines/default/engine/interface/ActorTalents.lua",
        "Talent activation, callbacks, and cooldown semantics are here.",
    ),
    (
        "Timed effects",
        "game/engines/default/engine/interface/ActorTemporaryEffects.lua",
        "Buff/debuff application and removal semantics are here.",
    ),
    (
        "ToME damage definitions",
        "game/modules/tome/data/damage_types.lua",
        "Module-specific damage type behavior and status side effects are defined here.",
    ),
)


@dataclass(frozen=True, slots=True)
class DocumentRef:
    path: str
    source: str
    rel_path: str
    suffix: str
    loader: Callable[[], str]


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    line: int
    text: str
    source: str


class ToolError(RuntimeError):
    pass


class TomeContentStore:
    def __init__(self, *, source_root: Path | None = None, install_root: Path | None = None) -> None:
        self.source_root = source_root or _default_source_root()
        self.install_root = install_root or Path(os.environ.get("TOME_INSTALL_ROOT", str(DEFAULT_INSTALL_ROOT)))

    def inventory(self) -> dict[str, Any]:
        source_root = self.source_root
        install_root = self.install_root
        source_versions = self._source_versions()
        archive_versions = self._archive_versions()
        logs = {
            key: {
                "path": str(path),
                "exists": path.is_file(),
                "size": path.stat().st_size if path.is_file() else 0,
            }
            for key, path in self._log_paths().items()
        }
        return {
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "source_root": str(source_root) if source_root else "",
            "source_root_exists": bool(source_root and source_root.is_dir()),
            "source_versions": source_versions,
            "install_root": str(install_root),
            "install_root_exists": install_root.is_dir(),
            "archives": self._archive_summary(),
            "archive_versions": archive_versions,
            "logs": logs,
            "version_warning": self._version_warning(source_versions, archive_versions),
            "scopes": SCOPES,
        }

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            raise ToolError("query is required")
        if scope not in SCOPES:
            raise ToolError(f"scope must be one of: {', '.join(SCOPES)}")
        max_results = max(1, min(MAX_RESULTS, int(max_results)))
        matcher = _compile_matcher(query, regex=regex, case_sensitive=case_sensitive)
        hits: list[SearchHit] = []
        for doc in self.iter_documents(scope=scope):
            try:
                text = doc.loader()
            except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if matcher(line):
                    hits.append(
                        SearchHit(
                            path=doc.path,
                            line=line_no,
                            text=_collapse_ws(line),
                            source=doc.source,
                        )
                    )
                    if len(hits) >= max_results:
                        return hits
        return hits

    def read_document(self, path: str, *, start_line: int = 1, line_count: int = 80) -> str:
        doc = self.resolve_document(path)
        try:
            text = doc.loader()
        except OSError as exc:
            raise ToolError(f"could not read {path}: {exc}") from exc
        start_line = max(1, int(start_line))
        line_count = max(1, min(400, int(line_count)))
        lines = text.splitlines()
        start = min(start_line - 1, len(lines))
        end = min(len(lines), start + line_count)
        width = len(str(end if end else start_line))
        rendered = [f"{idx:>{width}}: {lines[idx - 1]}" for idx in range(start + 1, end + 1)]
        header = f"{doc.path} lines {start + 1}-{end} ({len(lines)} total)"
        return header + "\n" + "\n".join(rendered)

    def resolve_document(self, path: str) -> DocumentRef:
        path = path.strip()
        if path.startswith("source:"):
            rel = _safe_rel(path.removeprefix("source:"))
            return self._source_document(rel)
        if path.startswith("install:"):
            archive_name, inner_path = _split_archive_path(path.removeprefix("install:"))
            return self._archive_document(archive_name, inner_path)
        if path.startswith("log:"):
            return self._log_document(path.removeprefix("log:"))
        if _looks_like_log_name(path):
            return self._log_document(path)
        rel = _safe_rel(path)
        if self.source_root and (self.source_root / Path(rel)).is_file():
            return self._source_document(rel)
        for archive_name in self._archive_paths():
            archive_path = self._archive_paths()[archive_name]
            if archive_path.is_file():
                try:
                    with zipfile.ZipFile(archive_path) as zf:
                        if rel in zf.namelist():
                            return self._archive_document(archive_name, rel)
                except zipfile.BadZipFile:
                    continue
        raise ToolError(f"path is not available through the ToME content store: {path}")

    def find_lua_definitions(
        self,
        name: str,
        *,
        kind: str = "any",
        scope: str = "all",
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        name = name.strip()
        if not name:
            raise ToolError("name is required")
        if kind not in {"any", "talent", "entity", "effect", "function", "class"}:
            raise ToolError("kind must be any, talent, entity, effect, function, or class")
        needle = name.casefold()
        results: list[dict[str, Any]] = []
        max_results = max(1, min(MAX_RESULTS, int(max_results)))
        for doc in self.iter_documents(scope=scope):
            if doc.suffix != ".lua":
                continue
            try:
                text = doc.loader()
            except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
                continue
            for result in _lua_definition_hits(text, doc.path, kind, needle):
                results.append(result)
                if len(results) >= max_results:
                    return results
        return results

    def battle_research(
        self,
        *,
        topic: str = "overview",
        query: str = "",
        max_results: int = 40,
    ) -> dict[str, Any]:
        topic = (topic or "overview").strip().casefold()
        if topic not in BATTLE_RESEARCH_QUERIES:
            topic = "overview"
        queries = [query.strip()] if query.strip() else list(BATTLE_RESEARCH_QUERIES[topic])
        per_query_limit = max(3, min(12, max_results // max(1, len(queries))))
        query_hits: dict[str, list[dict[str, Any]]] = {}
        for item in queries:
            hits = self.search(item, scope="combat", regex=False, case_sensitive=False, max_results=per_query_limit)
            query_hits[item] = [_hit_dict(hit) for hit in hits]
        return {
            "topic": topic,
            "anchors": self._battle_anchors(),
            "queries": queries,
            "hits": query_hits,
            "notes": [
                "Use Combat.lua:attackTargetWith as the melee path before changing quick threat math.",
                "DamageType projectors and ActorProject matter for non-weapon talents.",
                "The current Python simulator is a conservative estimator, not a faithful engine replica.",
            ],
        }

    def read_game_log(self, *, log_name: str = "te4", lines: int = 120, filter_text: str = "") -> str:
        log_name = (log_name or "te4").casefold()
        lines = max(1, min(1000, int(lines)))
        if log_name == "all":
            chunks = []
            for name in LOG_FILES:
                chunks.append(self.read_game_log(log_name=name, lines=lines, filter_text=filter_text))
            return "\n\n".join(chunks)
        doc = self._log_document(log_name)
        try:
            text = doc.loader()
        except OSError as exc:
            raise ToolError(f"could not read log {log_name}: {exc}") from exc
        all_lines = text.splitlines()
        if filter_text:
            needle = filter_text.casefold()
            all_lines = [line for line in all_lines if needle in line.casefold()]
        tail = all_lines[-lines:]
        return f"{doc.path} last {len(tail)} line(s)\n" + "\n".join(tail)

    def iter_documents(self, *, scope: str = "all") -> Iterator[DocumentRef]:
        if scope == "logs":
            yield from self._iter_logs()
            return
        yield from self._iter_source_tree(scope=scope)
        yield from self._iter_archives(scope=scope)
        if scope == "all":
            yield from self._iter_logs()

    def _iter_source_tree(self, *, scope: str) -> Iterator[DocumentRef]:
        root = self.source_root
        if not root or not root.is_dir():
            return
        paths = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: _doc_priority(path.relative_to(root).as_posix()),
        )
        for path in paths:
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if not _is_text_path(rel):
                continue
            if not _path_in_scope(rel, scope):
                continue
            yield DocumentRef(
                path=f"source:{rel}",
                source="source",
                rel_path=rel,
                suffix=path.suffix.casefold(),
                loader=lambda p=path: _read_text_file(p),
            )

    def _iter_archives(self, *, scope: str) -> Iterator[DocumentRef]:
        for archive_name, archive_path in self._archive_paths().items():
            if not archive_path.is_file():
                continue
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    names = sorted(zf.namelist(), key=lambda name: _doc_priority(_archive_source_rel(archive_name, name)))
            except zipfile.BadZipFile:
                continue
            for inner in names:
                if inner.endswith("/") or not _is_text_path(inner):
                    continue
                if not _archive_path_in_scope(archive_name, inner, scope):
                    continue
                yield DocumentRef(
                    path=f"install:{archive_name}:{inner}",
                    source="install",
                    rel_path=inner,
                    suffix=PurePosixPath(inner).suffix.casefold(),
                    loader=lambda a=archive_path, i=inner: _read_zip_text(a, i),
                )

    def _iter_logs(self) -> Iterator[DocumentRef]:
        for log_name in LOG_FILES:
            try:
                yield self._log_document(log_name)
            except ToolError:
                continue

    def _source_document(self, rel: str) -> DocumentRef:
        if not self.source_root:
            raise ToolError("no source root is configured")
        path = self.source_root / Path(rel)
        if not path.is_file():
            raise ToolError(f"source path not found: {rel}")
        if not _is_text_path(rel):
            raise ToolError(f"source path is not a supported text file: {rel}")
        return DocumentRef(
            path=f"source:{rel}",
            source="source",
            rel_path=rel,
            suffix=path.suffix.casefold(),
            loader=lambda: _read_text_file(path),
        )

    def _archive_document(self, archive_name: str, inner_path: str) -> DocumentRef:
        archives = self._archive_paths()
        if archive_name not in archives:
            raise ToolError(f"unknown archive {archive_name}")
        archive_path = archives[archive_name]
        if not archive_path.is_file():
            raise ToolError(f"archive not found: {archive_path}")
        inner_path = _safe_rel(inner_path)
        if not _is_text_path(inner_path):
            raise ToolError(f"archive path is not a supported text file: {inner_path}")
        try:
            with zipfile.ZipFile(archive_path) as zf:
                if inner_path not in zf.namelist():
                    raise ToolError(f"{inner_path} not found in {archive_name}")
        except zipfile.BadZipFile as exc:
            raise ToolError(f"archive is not readable: {archive_path}") from exc
        return DocumentRef(
            path=f"install:{archive_name}:{inner_path}",
            source="install",
            rel_path=inner_path,
            suffix=PurePosixPath(inner_path).suffix.casefold(),
            loader=lambda: _read_zip_text(archive_path, inner_path),
        )

    def _log_document(self, log_name: str) -> DocumentRef:
        log_paths = self._log_paths()
        log_name = log_name.strip().casefold()
        if log_name in LOG_FILES:
            path = log_paths[log_name]
            label = log_name
        else:
            rel = _safe_rel(log_name)
            path = self.install_root / Path(rel)
            label = PurePosixPath(rel).name
        if not path.is_file():
            raise ToolError(f"log file not found: {path}")
        return DocumentRef(
            path=f"log:{label}",
            source="log",
            rel_path=path.name,
            suffix=path.suffix.casefold(),
            loader=lambda: _read_text_file(path),
        )

    def _archive_paths(self) -> dict[str, Path]:
        engine_archives = sorted((self.install_root / "game" / "engines").glob("te4-*.teae"))
        archive_paths = {
            "tome.team": self.install_root / "game" / "modules" / "tome.team",
            "boot-te4.team": self.install_root / "game" / "modules" / "boot-te4.team",
        }
        if engine_archives:
            archive_paths[engine_archives[-1].name] = engine_archives[-1]
        return archive_paths

    def _archive_summary(self) -> dict[str, dict[str, Any]]:
        summary = {}
        for name, path in self._archive_paths().items():
            summary[name] = {
                "path": str(path),
                "exists": path.is_file(),
                "size": path.stat().st_size if path.is_file() else 0,
            }
        return summary

    def _log_paths(self) -> dict[str, Path]:
        return {name: self.install_root / filename for name, filename in LOG_FILES.items()}

    def _source_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        root = self.source_root
        if not root or not root.is_dir():
            return versions
        versions["module"] = _extract_version_from_text(root / "game" / "modules" / "tome" / "init.lua", "version")
        versions["engine"] = _extract_version_from_text(
            root / "game" / "engines" / "default" / "engine" / "version.lua",
            "engine.version",
        )
        return {key: value for key, value in versions.items() if value}

    def _archive_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        archives = self._archive_paths()
        tome_archive = archives.get("tome.team")
        if tome_archive and tome_archive.is_file():
            versions["module"] = _extract_version_from_zip(tome_archive, "mod/init.lua", "version")
        engine_archive = next((path for name, path in archives.items() if name.endswith(".teae")), None)
        if engine_archive and engine_archive.is_file():
            versions["engine"] = _extract_version_from_zip(engine_archive, "engine/version.lua", "engine.version")
        return {key: value for key, value in versions.items() if value}

    def _version_warning(self, source_versions: dict[str, str], archive_versions: dict[str, str]) -> str:
        warnings = []
        for key in ("module", "engine"):
            source = source_versions.get(key)
            archive = archive_versions.get(key)
            if source and archive and source != archive:
                warnings.append(f"{key} source is {source}, installed archive is {archive}")
        return "; ".join(warnings)

    def _battle_anchors(self) -> list[dict[str, Any]]:
        anchors = []
        for label, rel_path, note in BATTLE_ANCHORS:
            candidates = [f"source:{rel_path}"]
            archive_name = "tome.team" if rel_path.startswith("game/modules/tome/") else self._engine_archive_name()
            if archive_name:
                inner = _archive_inner_for_source_rel(rel_path)
                candidates.append(f"install:{archive_name}:{inner}")
            available = []
            for candidate in candidates:
                try:
                    self.resolve_document(candidate)
                except ToolError:
                    continue
                available.append(candidate)
            anchors.append({"label": label, "path": available[0] if available else candidates[0], "note": note})
        return anchors

    def _engine_archive_name(self) -> str:
        return next((name for name in self._archive_paths() if name.endswith(".teae")), "")


def _default_source_root() -> Path | None:
    env_path = os.environ.get("TOME_MCP_SOURCE_ROOT", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        return path if path.is_dir() else path
    for candidate in DEFAULT_SOURCE_ROOTS:
        if candidate.is_dir():
            return candidate
    return None


def _read_text_file(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise OSError(f"text file is too large for MCP read: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _read_zip_text(archive_path: Path, inner_path: str) -> str:
    with zipfile.ZipFile(archive_path) as zf:
        info = zf.getinfo(inner_path)
        if info.file_size > MAX_TEXT_BYTES:
            raise OSError(f"archive member is too large for MCP read: {inner_path}")
        return zf.read(inner_path).decode("utf-8", errors="replace")


def _is_text_path(rel_path: str) -> bool:
    rel = PurePosixPath(rel_path.replace("\\", "/"))
    suffix = rel.suffix.casefold()
    if suffix in BINARY_SUFFIXES:
        return False
    if suffix and suffix not in TEXT_SUFFIXES:
        return False
    parts = {part.casefold() for part in rel.parts}
    return not bool(parts & SKIP_DIR_NAMES)


def _path_in_scope(rel_path: str, scope: str) -> bool:
    if scope == "all":
        return True
    rel = rel_path.replace("\\", "/").casefold()
    if scope == "engine":
        return rel.startswith(("game/engines/default/", "src/", "bootstrap/", "game/loader/"))
    if scope == "module":
        return rel.startswith(("game/modules/tome/", "game/profile-thread/"))
    if scope == "combat":
        return _is_combat_path(rel)
    if scope == "addons":
        return "/addons/" in rel or rel.startswith("game/modules/example")
    if scope == "logs":
        return False
    return True


def _archive_path_in_scope(archive_name: str, inner_path: str, scope: str) -> bool:
    if scope == "all":
        return True
    rel = _archive_source_rel(archive_name, inner_path).casefold()
    if scope == "engine":
        return archive_name.endswith(".teae") or rel.startswith(("game/engines/default/", "game/loader/"))
    if scope == "module":
        return archive_name == "tome.team" or rel.startswith("game/profile-thread/")
    if scope == "combat":
        return _is_combat_path(rel)
    if scope == "addons":
        return "addon" in rel or archive_name == "boot-te4.team"
    return False


def _archive_source_rel(archive_name: str, inner_path: str) -> str:
    inner = inner_path.replace("\\", "/")
    if archive_name == "tome.team":
        if inner.startswith("mod/"):
            return "game/modules/tome/" + inner.removeprefix("mod/")
        return "game/modules/tome/" + inner
    if archive_name.endswith(".teae"):
        return "game/engines/default/" + inner
    if archive_name == "boot-te4.team":
        return "game/modules/boot-te4/" + inner
    return inner


def _archive_inner_for_source_rel(rel_path: str) -> str:
    rel = rel_path.replace("\\", "/")
    if rel.startswith("game/modules/tome/"):
        inner = rel.removeprefix("game/modules/tome/")
        if inner in {"init.lua", "load.lua"} or inner.startswith(("class/", "dialogs/", "ai/")):
            return "mod/" + inner
        return inner
    if rel.startswith("game/engines/default/"):
        return rel.removeprefix("game/engines/default/")
    return rel


def _is_combat_path(rel: str) -> bool:
    rel = rel.replace("\\", "/").casefold()
    combat_terms = (
        "actor",
        "archery",
        "combat",
        "damage",
        "effect",
        "life",
        "npc",
        "project",
        "resource",
        "talent",
        "temporary",
    )
    return any(term in rel for term in combat_terms)


def _doc_priority(rel_path: str) -> tuple[int, str]:
    rel = rel_path.replace("\\", "/").casefold()
    priority_markers = (
        "game/modules/tome/class/interface/combat.lua",
        "game/modules/tome/class/actor.lua",
        "game/engines/default/engine/damagetype.lua",
        "game/modules/tome/data/damage_types.lua",
        "game/engines/default/engine/interface/actorproject.lua",
        "game/engines/default/engine/interface/actorlife.lua",
        "game/engines/default/engine/interface/actortalents.lua",
        "game/engines/default/engine/interface/actortemporaryeffects.lua",
    )
    for index, marker in enumerate(priority_markers):
        if rel.endswith(marker):
            return (index, rel)
    if "/class/" in rel or "/engine/interface/" in rel:
        return (50, rel)
    if "/data/talents/" in rel or "/data/timed_effects/" in rel:
        return (80, rel)
    return (100, rel)


def _safe_rel(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ToolError(f"unsafe relative path: {path}")
    return pure.as_posix()


def _split_archive_path(value: str) -> tuple[str, str]:
    archive_name, sep, inner_path = value.partition(":")
    if not sep:
        raise ToolError("install paths must be install:<archive>:<inner path>")
    return archive_name, _safe_rel(inner_path)


def _looks_like_log_name(path: str) -> bool:
    value = path.casefold()
    return value in LOG_FILES or value in LOG_FILES.values()


def _compile_matcher(query: str, *, regex: bool, case_sensitive: bool) -> Callable[[str], bool]:
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ToolError(f"invalid regex: {exc}") from exc
        return lambda line: bool(pattern.search(line))
    needle = query if case_sensitive else query.casefold()
    if case_sensitive:
        return lambda line: needle in line
    return lambda line: needle in line.casefold()


def _collapse_ws(text: str, limit: int = 220) -> str:
    collapsed = re.sub(r"\s+", " ", text.strip())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _extract_version_from_text(path: Path, field_name: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _extract_version_from_string(text, field_name)


def _extract_version_from_zip(archive_path: Path, inner_path: str, field_name: str) -> str:
    try:
        text = _read_zip_text(archive_path, inner_path)
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    return _extract_version_from_string(text, field_name)


def _extract_version_from_string(text: str, field_name: str) -> str:
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*=\s*\{{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
    match = pattern.search(text)
    if not match:
        return ""
    return ".".join(match.groups())


def _lua_definition_hits(text: str, path: str, kind: str, needle: str) -> Iterator[dict[str, Any]]:
    if kind in {"any", "talent"}:
        yield from _lua_block_definition_hits(text, path, "newTalent", "talent", needle)
    if kind in {"any", "entity"}:
        yield from _lua_block_definition_hits(text, path, "newEntity", "entity", needle)
    if kind in {"any", "effect"}:
        yield from _lua_block_definition_hits(text, path, "newEffect", "effect", needle)
    if kind in {"any", "function"}:
        yield from _lua_function_hits(text, path, needle)
    if kind in {"any", "class"}:
        yield from _lua_class_hits(text, path, needle)


def _lua_block_definition_hits(text: str, path: str, opener: str, kind: str, needle: str) -> Iterator[dict[str, Any]]:
    for start, block in _iter_lua_blocks(text, opener):
        name = _field_string(block, "name")
        symbol = _field_string(block, "id") or _field_string(block, "define_as") or name
        haystack = f"{name}\n{symbol}\n{block[:800]}".casefold()
        if needle not in haystack:
            continue
        yield {
            "kind": kind,
            "path": path,
            "line": _line_for_offset(text, start),
            "name": name,
            "symbol": symbol,
            "preview": _preview_block(block),
        }


def _lua_function_hits(text: str, path: str, needle: str) -> Iterator[dict[str, Any]]:
    pattern = re.compile(r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)\s*\(", re.MULTILINE)
    for match in pattern.finditer(text):
        symbol = match.group(1)
        if needle not in symbol.casefold():
            continue
        line = _line_for_offset(text, match.start())
        yield {
            "kind": "function",
            "path": path,
            "line": line,
            "name": symbol,
            "symbol": symbol,
            "preview": _line_preview(text, line),
        }


def _lua_class_hits(text: str, path: str, needle: str) -> Iterator[dict[str, Any]]:
    pattern = re.compile(r'class\.inherit\s*\(\s*"([^"]+)"|module\s*\(\s*"([^"]+)"')
    for match in pattern.finditer(text):
        symbol = match.group(1) or match.group(2) or ""
        if needle not in symbol.casefold() and needle not in path.casefold():
            continue
        line = _line_for_offset(text, match.start())
        yield {
            "kind": "class",
            "path": path,
            "line": line,
            "name": symbol,
            "symbol": symbol,
            "preview": _line_preview(text, line),
        }


def _iter_lua_blocks(text: str, opener: str) -> Iterator[tuple[int, str]]:
    pattern = re.compile(rf"\b{re.escape(opener)}\s*\{{")
    for match in pattern.finditer(text):
        depth = 0
        in_string = ""
        escape = False
        index = match.end() - 1
        while index < len(text):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == in_string:
                    in_string = ""
            else:
                if char in {'"', "'"}:
                    in_string = char
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        yield match.start(), text[match.start() : index + 1]
                        break
            index += 1


def _field_string(block: str, field_name: str) -> str:
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*=\s*(?:_t)?\"([^\"]+)\"")
    match = pattern.search(block)
    return match.group(1).strip() if match else ""


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _preview_block(block: str) -> str:
    lines = [_collapse_ws(line, limit=160) for line in block.splitlines()[:10]]
    return "\n".join(line for line in lines if line)


def _line_preview(text: str, line: int, radius: int = 2) -> str:
    lines = text.splitlines()
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end))


def _hit_dict(hit: SearchHit) -> dict[str, Any]:
    return {"path": hit.path, "line": hit.line, "text": hit.text, "source": hit.source}


def _text_response(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _json_text_response(data: Any) -> dict[str, Any]:
    return _text_response(json.dumps(data, indent=2, ensure_ascii=False))


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "source_inventory",
            "description": "Report the ToME source tree, installed archives, versions, and log files available.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "search_source",
            "description": "Search ToME/T-Engine source, installed archives, and optionally logs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string", "enum": list(SCOPES), "default": "all"},
                    "regex": {"type": "boolean", "default": False},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 50},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_source",
            "description": "Read a line-numbered source/archive/log path returned by the search tools.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": 400, "default": 80},
                },
                "required": ["path"],
            },
        },
        {
            "name": "find_lua_definitions",
            "description": "Find Lua functions, classes, newTalent, newEntity, and newEffect blocks by name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["any", "talent", "entity", "effect", "function", "class"],
                        "default": "any",
                    },
                    "scope": {"type": "string", "enum": list(SCOPES), "default": "all"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 50},
                },
                "required": ["name"],
            },
        },
        {
            "name": "battle_simulator_research",
            "description": "Run a curated combat-formula research pass for fixing the Python battle simulator.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": list(BATTLE_RESEARCH_QUERIES),
                        "default": "overview",
                    },
                    "query": {"type": "string", "default": ""},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 40},
                },
            },
        },
        {
            "name": "read_game_log",
            "description": "Read the tail of te4_log.txt, te4_log_web.txt, debug.log, or all logs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "log_name": {"type": "string", "enum": ["te4", "web", "debug", "all"], "default": "te4"},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 120},
                    "filter_text": {"type": "string", "default": ""},
                },
            },
        },
    ]


def call_tool(store: TomeContentStore, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments or {}
    if name == "source_inventory":
        return _json_text_response(store.inventory())
    if name == "search_source":
        hits = store.search(
            str(args.get("query", "")),
            scope=str(args.get("scope", "all")),
            regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("case_sensitive", False)),
            max_results=int(args.get("max_results", 50)),
        )
        return _json_text_response([_hit_dict(hit) for hit in hits])
    if name == "read_source":
        return _text_response(
            store.read_document(
                str(args.get("path", "")),
                start_line=int(args.get("start_line", 1)),
                line_count=int(args.get("line_count", 80)),
            )
        )
    if name == "find_lua_definitions":
        results = store.find_lua_definitions(
            str(args.get("name", "")),
            kind=str(args.get("kind", "any")),
            scope=str(args.get("scope", "all")),
            max_results=int(args.get("max_results", 50)),
        )
        return _json_text_response(results)
    if name == "battle_simulator_research":
        return _json_text_response(
            store.battle_research(
                topic=str(args.get("topic", "overview")),
                query=str(args.get("query", "")),
                max_results=int(args.get("max_results", 40)),
            )
        )
    if name == "read_game_log":
        return _text_response(
            store.read_game_log(
                log_name=str(args.get("log_name", "te4")),
                lines=int(args.get("lines", 120)),
                filter_text=str(args.get("filter_text", "")),
            )
        )
    raise ToolError(f"unknown tool: {name}")


class McpServer:
    def __init__(self, store: TomeContentStore) -> None:
        self.store = store

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": _tool_schema()})
        if method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(self.store, str(params.get("name", "")), params.get("arguments") or {})
            except ToolError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            return self._result(request_id, result)
        return self._error(request_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_stdio(store: TomeContentStore) -> None:
    server = McpServer(store)
    for message in _read_json_messages(sys.stdin.buffer):
        try:
            response = server.handle(message)
        except Exception as exc:  # noqa: BLE001
            response = McpServer._error(message.get("id"), -32603, f"internal error: {exc}")
        if response is not None:
            _write_json_message(response)


def _read_json_messages(stream) -> Iterator[dict[str, Any]]:
    pending_header: bytes | None = None
    while True:
        line = pending_header if pending_header is not None else stream.readline()
        pending_header = None
        if not line:
            return
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                blank = stream.readline()
                if blank in {b"\r\n", b"\n", b""}:
                    break
            payload = stream.read(length)
            yield json.loads(payload.decode("utf-8"))
            continue
        yield json.loads(stripped.decode("utf-8"))


def _write_json_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _build_store(args: argparse.Namespace) -> TomeContentStore:
    source_root = Path(args.source_root).expanduser() if args.source_root else None
    install_root = Path(args.install_root).expanduser() if args.install_root else None
    return TomeContentStore(source_root=source_root, install_root=install_root)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local MCP server for ToME/T-Engine source research.")
    parser.add_argument("--stdio", action="store_true", help="Run as an MCP stdio server.")
    parser.add_argument("--source-root", default="", help="Path to a cloned t-engine4 source tree.")
    parser.add_argument("--install-root", default="", help="Path to the installed TalesMajEyal directory.")
    parser.add_argument("--list-tools", action="store_true", help="Print MCP tool schemas as JSON.")
    parser.add_argument("--call", default="", help="Call one tool directly for local testing.")
    parser.add_argument("--args", default="{}", help="JSON arguments for --call.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    store = _build_store(args)
    if args.list_tools:
        print(json.dumps(_tool_schema(), indent=2, ensure_ascii=False))
        return 0
    if args.call:
        try:
            call_args = json.loads(args.args)
            result = call_tool(store, args.call, call_args)
        except (json.JSONDecodeError, ToolError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.stdio:
        serve_stdio(store)
        return 0
    print(json.dumps(store.inventory(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
