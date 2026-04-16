"""
game_data/npc_db.py
-------------------
Parses NPC entity definitions from the ToME game archive (tome.team) and
exposes a name-keyed lookup of lore descriptions and sprite paths.

All 69 ``data/general/npcs/*.lua`` files are read from the zip archive on
first call; subsequent calls return the in-memory singleton.  A JSON cache
is written alongside this module so repeated launches skip the zip scan as
long as the archive has not been modified.

Usage::

    from game_data.npc_db import get_npc_db, NpcRecord

    db = get_npc_db()          # dict[str, NpcRecord]  — keys are lower-cased names
    rec = db.get("forest troll")
    if rec:
        print(rec.desc)        # lore text
        print(rec.image)       # "npc/troll_f.png"  (may be "")
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

_TOME_TEAM = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome.team"
)
_CACHE_FILE = Path(__file__).parent / "_npc_cache.json"

# ── Public types ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class NpcRecord:
    desc: str    # lore text, may be ""
    image: str   # "npc/xxx.png", may be ""


# ── Singleton ─────────────────────────────────────────────────────────────────

_db: dict[str, NpcRecord] | None = None


def get_npc_db() -> dict[str, NpcRecord]:
    """Return the NPC database, building or loading it on first call."""
    global _db
    if _db is None:
        _db = _load_or_build()
    return _db


# ── Load / build ──────────────────────────────────────────────────────────────

def _load_or_build() -> dict[str, NpcRecord]:
    if not _TOME_TEAM.exists():
        return {}

    # Re-use cached JSON if it is newer than the archive.
    if (
        _CACHE_FILE.exists()
        and _CACHE_FILE.stat().st_mtime > _TOME_TEAM.stat().st_mtime
    ):
        db = _load_cache()
        if db:
            return db

    db = _build_db()
    _save_cache(db)
    return db


def _build_db() -> dict[str, NpcRecord]:
    db: dict[str, NpcRecord] = {}
    try:
        with zipfile.ZipFile(_TOME_TEAM) as zf:
            all_names = zf.namelist()
            # Parse generic NPC families first (data/general/npcs/*.lua)
            # then zone-specific files (data/zones/*/npcs.lua).
            # Zone entries take priority — they override generic ones when
            # a boss variant of the same name has a more specific image.
            npc_paths = [
                n for n in all_names
                if "general/npcs/" in n and n.endswith(".lua")
            ]
            zone_paths = [
                n for n in all_names
                if re.match(r"data/zones/[^/]+/npcs\.lua", n)
            ]
            for path in npc_paths + zone_paths:
                lua = zf.read(path).decode("utf-8", errors="replace")
                for name, record in _parse_lua(lua):
                    db[name.lower()] = record
    except Exception:  # noqa: BLE001 — degrade silently
        pass
    return db


# ── Lua parsing ───────────────────────────────────────────────────────────────

def _split_entities(lua: str) -> list[str]:
    """
    Return each ``newEntity{ ... }`` block as a raw string.
    Uses a depth counter to handle nested ``{...}`` tables.
    """
    results: list[str] = []
    pat = re.compile(r"\bnewEntity\s*\{")
    for m in pat.finditer(lua):
        depth = 0
        i = m.end() - 1  # position of the opening '{'
        while i < len(lua):
            c = lua[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    results.append(lua[m.start() : i + 1])
                    break
            i += 1
    return results


_RE_NAME       = re.compile(r'\bname\s*=\s*"([^"]+)"')
_RE_DESC_ML    = re.compile(r'desc\s*=\s*_t\[\[(.*?)\]\]', re.DOTALL)
_RE_DESC_SQ    = re.compile(r'desc\s*=\s*_t"([^"]+)"')
_RE_IMG        = re.compile(r'\bimage\s*=\s*"(npc/[^"]+\.png)"')
_RE_ADD_MOS    = re.compile(
    r'add_mos\s*=\s*\{\{[^}]*image\s*=\s*"(npc/[^"]+\.png)"'
)
# Matches BASE_NPC_… style define_as values — these are anonymous templates,
# not real entities.  Zone bosses also have define_as but it's an identifier
# like "NORGOS" or "THE_MASTER" — they still have a name and should be indexed.
_RE_BASE_TMPL  = re.compile(r'define_as\s*=\s*"BASE_')


def _parse_lua(lua: str) -> list[tuple[str, NpcRecord]]:
    """Extract ``(name, NpcRecord)`` pairs from a Lua NPC file."""
    results: list[tuple[str, NpcRecord]] = []

    for block in _split_entities(lua):
        # Skip anonymous base templates (define_as = "BASE_NPC_…")
        if _RE_BASE_TMPL.search(block):
            continue

        name_m = _RE_NAME.search(block)
        if not name_m:
            continue
        name = name_m.group(1)

        # Description — prefer multiline [[...]], fall back to single-line "..."
        desc_m = _RE_DESC_ML.search(block)
        if desc_m:
            desc = desc_m.group(1).strip()
        else:
            desc_sq = _RE_DESC_SQ.search(block)
            desc = desc_sq.group(1).strip() if desc_sq else ""

        # Sprite — prefer direct image field, fall back to add_mos
        img_m = _RE_IMG.search(block)
        if img_m:
            image = img_m.group(1)
        else:
            add_m = _RE_ADD_MOS.search(block)
            image = add_m.group(1) if add_m else ""

        results.append((name, NpcRecord(desc=desc, image=image)))

    return results


# ── JSON cache helpers ────────────────────────────────────────────────────────

def _load_cache() -> dict[str, NpcRecord]:
    try:
        raw: dict[str, dict[str, str]] = json.loads(
            _CACHE_FILE.read_text(encoding="utf-8")
        )
        return {k: NpcRecord(desc=v["desc"], image=v["image"]) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(db: dict[str, NpcRecord]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        raw = {k: {"desc": v.desc, "image": v.image} for k, v in db.items()}
        _CACHE_FILE.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
