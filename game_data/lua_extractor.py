"""
game_data/lua_extractor.py
--------------------------
Shared helpers for parsing Lua data files out of the ToME ``tome.team`` zip
archive. The NPC, talent, and prodigy databases all follow the same shape:

    1. Locate ``tome.team``
    2. Read specific ``.lua`` paths from the zip
    3. Split each file into ``newX { ... }`` blocks
    4. Regex-extract fields from each block

This module centralises steps 1 and 3 plus a handful of common regexes.
Each caller (``npc_db``, ``talent_db``, ``prodigy_db``) still owns its own
field extractors — block-level structure is the only genuinely shared bit.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

# ── Locating tome.team ───────────────────────────────────────────────────────

# Steam install path. The shipped zip is stable across launches.
_STEAM_PATH: Final[Path] = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal"
    r"\game\modules\tome.team"
)


def find_tome_team() -> Path | None:
    """Return a path to ``tome.team`` if one can be found, else ``None``.

    Checks the Steam install first (stable, always present if the game is
    installed via Steam). Falls back to common extracted-copy locations
    the engine uses at runtime (TEMP, LOCALAPPDATA/APPDATA\\T-Engine).
    """
    if _STEAM_PATH.is_file():
        return _STEAM_PATH

    for env_var, suffix in (
        ("TEMP", ""),
        ("TMP", ""),
        ("LOCALAPPDATA", r"T-Engine"),
        ("APPDATA", r"T-Engine"),
    ):
        root = os.environ.get(env_var, "")
        if not root:
            continue
        candidate = Path(root) / suffix / "tome.team" if suffix else Path(root) / "tome.team"
        if candidate.is_file():
            return candidate

    return None


# ── Block splitters ──────────────────────────────────────────────────────────


def iter_balanced_blocks(lua: str, opener: str) -> Iterator[str]:
    """Yield raw ``<opener>{ ... }`` blocks via brace-depth counting.

    Robust to nested ``{}`` inside the block body (common in ToME data).
    Returns the full match including the opener and both braces.

    Example::

        for block in iter_balanced_blocks(src, "newEntity"):
            if m := re.search(r'name\\s*=\\s*"([^"]+)"', block):
                ...
    """
    pat = re.compile(rf"\b{re.escape(opener)}\s*\{{")
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
                    yield lua[m.start() : i + 1]
                    break
            i += 1


def iter_regex_blocks(lua: str, opener: str) -> list[str]:
    """Yield ``<opener>{ ... }`` blocks using the faster "}^$" regex trick.

    Relies on the closing ``}`` sitting on a line by itself — which ToME's
    codebase is consistent about for top-level ``newTalent`` / ``uberTalent``
    definitions, though *not* for ``newEntity`` (nested tables commonly
    close on the same line as trailing fields).

    Faster than ``iter_balanced_blocks`` when the assumption holds. Prefer
    :func:`iter_balanced_blocks` if unsure.
    """
    pat = re.compile(rf"{re.escape(opener)}\s*\{{(.*?)^\}}", re.DOTALL | re.MULTILINE)
    return [f"{opener}{{{body}\n}}" for body in pat.findall(lua)]


# ── Common regex primitives ──────────────────────────────────────────────────

# Optional ``_t`` wrapper (I18N marker) is tolerated in front of strings.
RE_NAME: Final = re.compile(r'\bname\s*=\s*(?:_t)?"([^"]+)"')
RE_SHORT_NAME: Final = re.compile(r'\bshort_name\s*=\s*"([^"]+)"')
RE_DESC_BLOCK: Final = re.compile(r"\bdesc\s*=\s*(?:_t)?\[\[(.*?)\]\]", re.DOTALL)
RE_DESC_LINE: Final = re.compile(r'\bdesc\s*=\s*(?:_t)?"((?:[^"\\]|\\.)*)"')
RE_IMAGE: Final = re.compile(r'\bimage\s*=\s*"([^"]+)"')
RE_NOT_LISTED: Final = re.compile(r"\bnot_listed\s*=\s*true\b")


def name_to_tid(name: str) -> str:
    """Convert a talent display name to its ``T_*`` id (mirrors ToME's Lua rule)."""
    tid = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    return f"T_{tid}"
