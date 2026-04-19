"""
game_data/prodigy_db.py
-----------------------
Parse ToME prodigy talent definitions and expose the subset of metadata the
GUI needs to decide whether a prodigy should be shown and what to display in
the talent detail panel.

Unlike ordinary talent records, prodigies are wrapped by ``uberTalent`` in
``data/talents/uber/uber.lua``. Their effective type and base stat gate are
implied by the source file they live in, and special requirements often live
inside ``require = { special={...} }`` blocks. This module rebuilds those
relationships once and keeps an in-memory singleton.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from game_data.lua_extractor import (
    RE_DESC_BLOCK,
    RE_DESC_LINE,
    RE_IMAGE,
    RE_NAME,
    RE_NOT_LISTED,
    RE_SHORT_NAME,
    find_tome_team,
    iter_regex_blocks,
    name_to_tid,
)

_LOCAL_UBER_DIR = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "t-engine4-master"
    / "game"
    / "modules"
    / "tome"
    / "data"
    / "talents"
    / "uber"
)
_UBER_FILES: dict[str, tuple[str, tuple[tuple[str, int], ...]]] = {
    "str.lua": ("uber/strength", (("str", 50),)),
    "dex.lua": ("uber/dexterity", (("dex", 50),)),
    "const.lua": ("uber/constitution", (("con", 50),)),
    "mag.lua": ("uber/magic", (("mag", 50),)),
    "wil.lua": ("uber/willpower", (("wil", 50),)),
    "cun.lua": ("uber/cunning", (("cun", 50),)),
}

_RE_MODE = re.compile(r'\bmode\s*=\s*"([^"]+)"')
_RE_REQUIRE = re.compile(r"\brequire\s*=\s*\{")
_RE_SPECIAL = re.compile(r"\bspecial\d*\s*=\s*\{")
_RE_STAT = re.compile(r"\bstat\s*=\s*\{")
_RE_STAT_ENTRY = re.compile(r"\b(str|dex|con|mag|wil|cun)\s*=\s*(\d+)\b")
_RE_BIRTH_DESCRIPTORS = re.compile(r"\bbirth_descriptors\s*=\s*\{")
_RE_BIRTH_DESCRIPTOR_ENTRY = re.compile(r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\}')
_RE_KNOW_TALENT_TYPE = re.compile(r'self:knowTalentType\("([^"]+)"\)')
_RE_CLASS_EVOLUTION = re.compile(r'\bis_class_evolution\s*=\s*"([^"]+)"')
_RE_RACE_EVOLUTION_BLOCK = re.compile(r"\bis_race_evolution\s*=\s*function\b.*?\n\s*end\s*,", re.DOTALL)
_RE_INFO_BLOCK = re.compile(r"\binfo\s*=\s*function\b.*?\breturn\s*\(\[\[(.*?)\]\]\)", re.DOTALL)
_RE_INFO_LINE = re.compile(r'\binfo\s*=\s*function\b.*?\breturn\s*\(\s*(?:_t)?\"((?:[^"\\]|\\.)*)\"', re.DOTALL)


@dataclass(frozen=True, slots=True)
class ProdigyRecord:
    name: str
    talent_id: str
    talent_type: str
    stat_requirements: tuple[tuple[str, int], ...] = ()
    birth_descriptors: tuple[tuple[str, str], ...] = ()
    category_requirements: tuple[tuple[str, str], ...] = ()
    remaining_requirements: tuple[str, ...] = ()
    special_logic: tuple[str, ...] = ()
    class_evolution_for: str = ""
    race_evolution_logic: str = ""
    icon: str = ""
    mode: str = ""
    description: str = ""


_db: dict[str, ProdigyRecord] | None = None


def get_prodigy_db() -> dict[str, ProdigyRecord]:
    """Return prodigies keyed by engine id (``T_*``)."""
    global _db
    if _db is None:
        _db = _build_db()
    return _db


def _build_db() -> dict[str, ProdigyRecord]:
    if db := _build_db_from_archives():
        return db
    return _build_db_from_local_files()


def _build_db_from_local_files() -> dict[str, ProdigyRecord]:
    if not _LOCAL_UBER_DIR.is_dir():
        return {}

    sources: list[tuple[str, str]] = []
    for file_name in _UBER_FILES:
        path = _LOCAL_UBER_DIR / file_name
        if not path.is_file():
            continue
        try:
            sources.append((file_name, path.read_text(encoding="utf-8", errors="replace")))
        except Exception:  # noqa: BLE001
            continue
    return _build_db_from_sources(sources)


def _build_db_from_archives() -> dict[str, ProdigyRecord]:
    sources: list[tuple[str, str]] = []
    for archive_path in _iter_archive_paths():
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for member_name in zf.namelist():
                    member_path = PurePosixPath(member_name)
                    file_name = member_path.name
                    if (
                        file_name not in _UBER_FILES
                        or len(member_path.parts) < 4
                        or tuple(member_path.parts[-4:-1]) != ("data", "talents", "uber")
                    ):
                        continue
                    try:
                        sources.append((file_name, zf.read(member_name).decode("utf-8", errors="replace")))
                    except KeyError:
                        continue
        except Exception:  # noqa: BLE001
            continue
    return _build_db_from_sources(sources)


def _iter_archive_paths() -> list[Path]:
    tome_team = find_tome_team()
    if tome_team is None:
        return []

    archives: list[Path] = [tome_team]
    game_root = tome_team.parent.parent
    for folder_name in ("dlcs", "addons"):
        folder = game_root / folder_name
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in {".teaa", ".teaac", ".team"}:
                archives.append(path)

    seen: set[Path] = set()
    unique_archives: list[Path] = []
    for archive in archives:
        resolved = archive.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_archives.append(resolved)
    return unique_archives


def _build_db_from_sources(sources: list[tuple[str, str]]) -> dict[str, ProdigyRecord]:
    db: dict[str, ProdigyRecord] = {}
    for file_name, lua in sources:
        file_info = _UBER_FILES.get(file_name)
        if file_info is None:
            continue
        talent_type, base_stats = file_info
        for block in iter_regex_blocks(lua, "uberTalent"):
            record = _parse_block(block, talent_type, base_stats)
            if record is not None:
                db[record.talent_id] = record
    return db


def _parse_block(
    block: str,
    talent_type: str,
    base_stats: tuple[tuple[str, int], ...],
) -> ProdigyRecord | None:
    if RE_NOT_LISTED.search(block):
        return None

    name_match = RE_NAME.search(block)
    if not name_match:
        return None
    name = name_match.group(1).strip()
    if not name:
        return None

    stat_requirements = dict(base_stats)
    birth_descriptors: list[tuple[str, str]] = []
    category_requirements: list[tuple[str, str]] = []
    remaining_requirements: list[str] = []
    special_logic: list[str] = []
    class_evolution_for = ""
    race_evolution_logic = ""

    require_block = _extract_last_named_table(block, _RE_REQUIRE)
    if require_block:
        stat_block = _extract_named_table(require_block, _RE_STAT)
        if stat_block:
            for stat_key, raw_value in _RE_STAT_ENTRY.findall(stat_block):
                stat_requirements[stat_key] = max(stat_requirements.get(stat_key, 0), int(raw_value))

        birth_block = _extract_named_table(require_block, _RE_BIRTH_DESCRIPTORS)
        if birth_block:
            birth_descriptors.extend(_RE_BIRTH_DESCRIPTOR_ENTRY.findall(birth_block))

        for special_block in _iter_named_tables(require_block, _RE_SPECIAL):
            special_logic.append(special_block)
            description = _extract_requirement_text(special_block)
            category_types = _unique(_RE_KNOW_TALENT_TYPE.findall(special_block))
            for type_key in category_types:
                category_requirements.append((type_key, description))
            if description and not (category_types and _is_category_only_requirement(description, category_types)):
                remaining_requirements.append(description)

    if match := _RE_CLASS_EVOLUTION.search(block):
        class_evolution_for = match.group(1).strip()
    if match := _RE_RACE_EVOLUTION_BLOCK.search(block):
        race_evolution_logic = match.group(0).strip()

    return ProdigyRecord(
        name=name,
        talent_id=_extract_talent_id(block, name),
        talent_type=talent_type,
        stat_requirements=tuple(sorted(stat_requirements.items())),
        birth_descriptors=tuple(_unique(birth_descriptors)),
        category_requirements=tuple(_unique(category_requirements)),
        remaining_requirements=tuple(_unique(remaining_requirements)),
        special_logic=tuple(special_logic),
        class_evolution_for=class_evolution_for,
        race_evolution_logic=race_evolution_logic,
        icon=_extract_icon(block),
        mode=_extract_mode(block),
        description=_extract_info_description(block),
    )


def _extract_talent_id(block: str, name: str) -> str:
    if match := RE_SHORT_NAME.search(block):
        short_name = match.group(1).strip().upper()
        return short_name if short_name.startswith("T_") else f"T_{short_name}"
    return name_to_tid(name)


def _extract_icon(block: str) -> str:
    if not (match := RE_IMAGE.search(block)):
        return ""
    image = match.group(1).strip()
    if not image.endswith(".png"):
        return ""
    return PurePosixPath(image).name


def _extract_mode(block: str) -> str:
    if not (match := _RE_MODE.search(block)):
        return ""
    return match.group(1).strip().lower()


def _extract_info_description(block: str) -> str:
    if match := _RE_INFO_BLOCK.search(block):
        return _normalize_text(match.group(1))
    if match := _RE_INFO_LINE.search(block):
        return _normalize_text(bytes(match.group(1), "utf-8").decode("unicode_escape"))
    return ""


def _extract_requirement_text(block: str) -> str:
    if match := RE_DESC_BLOCK.search(block):
        return _normalize_text(match.group(1))
    if match := RE_DESC_LINE.search(block):
        return _normalize_text(bytes(match.group(1), "utf-8").decode("unicode_escape"))
    return ""


def _extract_named_table(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    return _extract_balanced_braces(text, match.end() - 1)


def _extract_last_named_table(text: str, pattern: re.Pattern[str]) -> str:
    matches = list(pattern.finditer(text))
    if not matches:
        return ""
    return _extract_balanced_braces(text, matches[-1].end() - 1)


def _iter_named_tables(text: str, pattern: re.Pattern[str]) -> list[str]:
    tables: list[str] = []
    for match in pattern.finditer(text):
        table = _extract_balanced_braces(text, match.end() - 1)
        if table:
            tables.append(table)
    return tables


def _extract_balanced_braces(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _normalize_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact = " ".join(line for line in lines if line)
    return re.sub(r"\s+", " ", compact).strip()


def _normalize_requirement_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _is_category_only_requirement(description: str, type_keys: list[str]) -> bool:
    normalized_desc = _normalize_requirement_key(description)
    for type_key in type_keys:
        _, _, leaf = type_key.partition("/")
        if normalized_desc == _normalize_requirement_key(leaf.replace("-", " ")):
            return True
        if normalized_desc == _normalize_requirement_key(type_key.replace("/", " ")):
            return True
    return False


def _unique[T](items: list[T]) -> list[T]:
    seen: set[T] = set()
    result: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
