"""
probe_entities.py
-----------------
Diagnostic: reads game.level to understand the entity table structure.
Run from Administrator terminal with game open and character loaded.

    python tools/probe_entities.py
"""
from __future__ import annotations

import struct
import sys
import os

# Add project root to path so we can import gui.memory_reader
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.memory_reader import (
    MemoryReader,
    _tab_find_strkey,
    _tab_get_table,
    _tab_get_number,
    _is_heap,
    _rpm,
    _ru32,
    _rf64,
    _LJ_TSTR,
    _LJ_TTAB,
    _LJ_TNUMX,
    _NODE_SIZE,
)


def _tab_get_string(h: int, tab_ptr: int, key: str) -> str | None:
    """Look up a string key and return the string value, or None."""
    node = _tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = _ru32(h, node + 4)
    if it != _LJ_TSTR:
        return None
    gcs = _ru32(h, node)
    if not gcs or not _is_heap(gcs):
        return None
    slen_raw = _rpm(h, gcs + 12, 4)
    if not slen_raw:
        return None
    slen = struct.unpack('<I', slen_raw)[0]
    if slen > 256:
        return None
    raw = _rpm(h, gcs + 16, slen)
    if not raw:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return None


def _tab_all_entries(h: int, tab_ptr: int) -> list[tuple[str, int, int]]:
    """
    Return all entries from hash part as (key_type, key_info, val_tvalue_addr).
    key_type is 'string', 'number', 'table', or 'other'.
    key_info is the string content (for strings) or raw value.
    """
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask    = _ru32(h, tab_ptr + 28)
    asize    = _ru32(h, tab_ptr + 24)
    arr_ptr  = _ru32(h, tab_ptr + 8)

    entries = []

    # Array part
    if arr_ptr and _is_heap(arr_ptr) and asize and asize < 100000:
        for i in range(asize):
            addr = arr_ptr + i * 8
            it = _ru32(h, addr + 4)
            if it == 0xFFFFFFFF:  # nil
                continue
            entries.append(("array_index", str(i), addr))

    # Hash part
    if node_ptr and _is_heap(node_ptr) and hmask is not None:
        total = min((hmask + 1) * _NODE_SIZE, 16 * 1024 * 1024)
        bulk = _rpm(h, node_ptr, total)
        if bulk:
            for i in range(hmask + 1):
                off = i * _NODE_SIZE
                val_it = struct.unpack_from('<I', bulk, off + 4)[0]
                if val_it == 0xFFFFFFFF:  # nil value = empty slot
                    continue
                key_it = struct.unpack_from('<I', bulk, off + 12)[0]
                key_lo = struct.unpack_from('<I', bulk, off + 8)[0]

                if key_it == _LJ_TSTR and _is_heap(key_lo):
                    slen_raw = _rpm(h, key_lo + 12, 4)
                    if slen_raw:
                        slen = struct.unpack('<I', slen_raw)[0]
                        if 0 < slen < 128:
                            raw = _rpm(h, key_lo + 16, slen)
                            if raw:
                                try:
                                    entries.append(("string", raw.decode('utf-8'), node_ptr + off))
                                except UnicodeDecodeError:
                                    entries.append(("string_bad", f"0x{key_lo:08X}", node_ptr + off))
                elif key_it < _LJ_TNUMX:
                    # Number key
                    nval = struct.unpack_from('<d', bulk, off + 8)[0]
                    entries.append(("number", str(nval), node_ptr + off))
                elif key_it == _LJ_TTAB:
                    entries.append(("table_key", f"0x{key_lo:08X}", node_ptr + off))
                else:
                    entries.append(("other", f"it=0x{key_it:08X}", node_ptr + off))

    return entries


def main() -> None:
    reader = MemoryReader()
    print("Attaching to t-engine.exe...")
    if not reader.attach():
        print("Failed to attach. Is the game running? Are you Administrator?")
        sys.exit(1)
    print("Attached.\n")

    h  = reader._handle
    gt = reader._global_table

    game_tab = _tab_get_table(h, gt, "game")
    if not game_tab:
        print("'game' not found in _G")
        sys.exit(1)

    level_tab = _tab_get_table(h, game_tab, "level")
    if not level_tab:
        print("'level' not found in game (are you in-game on a map?)")
        sys.exit(1)

    # ── Show level info ──
    print("=== game.level ===")
    level_id = _tab_get_string(h, level_tab, "id")
    print(f"  id = {level_id}")

    level_num = _tab_get_number(h, level_tab, "level")
    print(f"  level = {level_num}")

    level_name = _tab_get_string(h, level_tab, "name")
    print(f"  name = {level_name}")

    # ── Probe entities table ──
    print("\n=== game.level.entities ===")
    entities_node = _tab_find_strkey(h, level_tab, "entities")
    if entities_node is None:
        # Maybe it's called e_array or something else
        print("'entities' not found. Checking other keys...")
        entries = _tab_all_entries(h, level_tab)
        string_keys = [e[1] for e in entries if e[0] == "string"]
        print(f"  level table keys ({len(string_keys)}): {string_keys[:40]}")
        # Look for entity-related keys
        for candidate in ["entities", "e_array", "actors", "npcs", "foes"]:
            if candidate in string_keys:
                print(f"  Found: {candidate}")
        sys.exit(0)

    entities_it = _ru32(h, entities_node + 4)
    if entities_it != _LJ_TTAB:
        print(f"  'entities' is not a table (it=0x{entities_it:08X})")
        sys.exit(1)

    entities_tab = _ru32(h, entities_node)
    hmask = _ru32(h, entities_tab + 28)
    asize = _ru32(h, entities_tab + 24)
    print(f"  entities table: 0x{entities_tab:08X}  asize={asize}  hmask={hmask}")

    entries = _tab_all_entries(h, entities_tab)
    print(f"  total entries: {len(entries)}")
    print(f"  key types: ", end="")
    types = {}
    for ktype, _, _ in entries:
        types[ktype] = types.get(ktype, 0) + 1
    print(types)

    # ── Read first few entities ──
    print(f"\n=== First 10 entities ===")
    count = 0
    for ktype, kinfo, val_addr in entries:
        if count >= 10:
            break
        val_it = _ru32(h, val_addr + 4)
        if val_it != _LJ_TTAB:
            continue
        ent_tab = _ru32(h, val_addr)
        if not ent_tab or not _is_heap(ent_tab):
            continue

        name    = _tab_get_string(h, ent_tab, "name")
        rank    = _tab_get_string(h, ent_tab, "rank")
        life    = _tab_get_number(h, ent_tab, "life")
        maxlife = _tab_get_number(h, ent_tab, "max_life")
        faction = _tab_get_string(h, ent_tab, "faction")
        lvl     = _tab_get_number(h, ent_tab, "level")
        x       = _tab_get_number(h, ent_tab, "x")
        y       = _tab_get_number(h, ent_tab, "y")
        dead    = _tab_find_strkey(h, ent_tab, "dead")

        is_dead = False
        if dead is not None:
            dead_it = _ru32(h, dead + 4)
            is_dead = dead_it == 0xFFFFFFFD  # LJ_TTRUE

        print(f"\n  [{ktype}={kinfo}]")
        print(f"    name={name}  rank={rank}  level={lvl}")
        print(f"    hp={life:.0f}/{maxlife:.0f}" if life and maxlife else f"    hp=?/?")
        print(f"    faction={faction}  pos=({x},{y})  dead={is_dead}")

        # Check for combat stats
        armor   = _tab_get_number(h, ent_tab, "combat_armor")
        defense = _tab_get_number(h, ent_tab, "combat_def")
        phys_save = _tab_get_number(h, ent_tab, "combat_physresist")
        spell_save = _tab_get_number(h, ent_tab, "combat_spellresist")
        mental_save = _tab_get_number(h, ent_tab, "combat_mentalresist")
        print(f"    armor={armor}  def={defense}  saves=phys:{phys_save}/spell:{spell_save}/mental:{mental_save}")

        count += 1

    reader.detach()
    print("\nDone.")


if __name__ == "__main__":
    main()
