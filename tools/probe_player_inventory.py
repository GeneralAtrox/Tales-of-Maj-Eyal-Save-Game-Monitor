"""
probe_player_inventory.py
-------------------------
Diagnostic: inspect game.player for inventory/equipment/transmog tables.
Run from an Administrator terminal with Tales of Maj'Eyal open and a
character loaded.

    py -3 tools/probe_player_inventory.py
"""
from __future__ import annotations

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.memory_reader import (  # noqa: E402
    MemoryReader,
    _is_heap,
    _rpm,
    _ru32,
    _tab_dump_flat,
    _tab_get_ordered_tables,
    _tab_get_table,
    _tab_iter_table_values,
    _LJ_TFALSE,
    _LJ_TNIL,
    _LJ_TNUMX,
    _LJ_TSTR,
    _LJ_TTAB,
    _LJ_TTRUE,
    _NODE_SIZE,
)


def _tab_array_size(h: int, tab_ptr: int) -> int:
    return _ru32(h, tab_ptr + 24) or 0


def _tab_hmask(h: int, tab_ptr: int) -> int:
    return _ru32(h, tab_ptr + 28) or 0


def _decode_string_gc(h: int, gcs: int, *, max_len: int = 128) -> str | None:
    if not gcs or not _is_heap(gcs):
        return None
    slen_raw = _rpm(h, gcs + 12, 4)
    if not slen_raw:
        return None
    slen = struct.unpack("<I", slen_raw)[0]
    if slen <= 0 or slen > max_len:
        return None
    raw = _rpm(h, gcs + 16, slen)
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _value_preview(h: int, value_addr: int) -> str:
    itype = _ru32(h, value_addr + 4)
    if itype is None:
        return "<unreadable>"
    if itype == _LJ_TTRUE:
        return "true"
    if itype == _LJ_TFALSE:
        return "false"
    if itype == _LJ_TNIL:
        return "nil"
    if itype == _LJ_TSTR:
        gcs = _ru32(h, value_addr)
        return repr(_decode_string_gc(h, gcs, max_len=96) or "<string>")
    if itype == _LJ_TTAB:
        ptr = _ru32(h, value_addr)
        return f"<table 0x{ptr:08X}>" if ptr else "<table>"
    if itype < _LJ_TNUMX:
        raw = _rpm(h, value_addr, 8)
        if raw:
            try:
                return str(struct.unpack("<d", raw)[0])
            except struct.error:
                pass
    return f"<itype 0x{itype:08X}>"


def _table_string_keys(h: int, tab_ptr: int) -> list[str]:
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return []
    total = (hmask + 1) * _NODE_SIZE
    if total > 8 * 1024 * 1024:
        return []
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return []
    keys: list[str] = []
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        if val_it == _LJ_TNIL:
            continue
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != _LJ_TSTR:
            continue
        key_gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        key = _decode_string_gc(h, key_gcs)
        if key:
            keys.append(key)
    return sorted(set(keys))


def _table_entries(h: int, tab_ptr: int) -> list[tuple[str, int]]:
    node_ptr = _ru32(h, tab_ptr + 20)
    hmask = _ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not _is_heap(node_ptr):
        return []
    total = (hmask + 1) * _NODE_SIZE
    if total > 8 * 1024 * 1024:
        return []
    bulk = _rpm(h, node_ptr, total)
    if not bulk:
        return []

    entries: list[tuple[str, int]] = []
    for i in range(hmask + 1):
        off = i * _NODE_SIZE
        val_it = struct.unpack_from("<I", bulk, off + 4)[0]
        if val_it == _LJ_TNIL:
            continue
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        value_addr = node_ptr + off
        if key_it == _LJ_TSTR:
            key_gcs = struct.unpack_from("<I", bulk, off + 8)[0]
            key = _decode_string_gc(h, key_gcs)
            if key:
                entries.append((key, value_addr))
        elif key_it < _LJ_TNUMX:
            try:
                key_num = struct.unpack_from("<d", bulk, off + 8)[0]
                entries.append((str(int(key_num) if key_num == int(key_num) else key_num), value_addr))
            except (struct.error, ValueError):
                continue
    return entries


def _item_like_snapshot(h: int, tab_ptr: int) -> dict[str, object]:
    flat = _tab_dump_flat(h, tab_ptr)
    interesting: dict[str, object] = {}
    for key in (
        "name",
        "type",
        "subtype",
        "slot",
        "desc",
        "material_level",
        "encumber",
        "encumbrance",
        "power_source",
        "defined_as",
        "identified",
        "unique",
        "unided_name",
    ):
        if key in flat:
            interesting[key] = flat[key]
    if not interesting:
        interesting = flat
    if "desc" in interesting and isinstance(interesting["desc"], str):
        interesting["desc"] = " ".join(str(interesting["desc"]).split())[:120]
    return interesting


def _probe_container(h: int, tab_ptr: int, label: str) -> dict[str, object]:
    result: dict[str, object] = {
        "label": label,
        "ptr": f"0x{tab_ptr:08X}",
        "asize": _tab_array_size(h, tab_ptr),
        "hmask": _tab_hmask(h, tab_ptr),
        "string_keys": _table_string_keys(h, tab_ptr)[:40],
    }

    ordered = _tab_get_ordered_tables(h, tab_ptr)
    table_values = _tab_iter_table_values(h, tab_ptr)
    sample_tables = ordered or table_values
    result["ordered_table_count"] = len(ordered)
    result["table_value_count"] = len(table_values)

    samples: list[dict[str, object]] = []
    for idx, sub_ptr in enumerate(sample_tables[:5], start=1):
        sub_keys = _table_string_keys(h, sub_ptr)
        samples.append(
            {
                "index": idx,
                "ptr": f"0x{sub_ptr:08X}",
                "keys": sub_keys[:24],
                "item_like": _item_like_snapshot(h, sub_ptr),
                "ordered_children": len(_tab_get_ordered_tables(h, sub_ptr)),
                "table_children": len(_tab_iter_table_values(h, sub_ptr)),
            }
        )
    result["samples"] = samples
    return result


def _candidate_subtables(h: int, player_tab: int) -> list[tuple[str, int]]:
    player_keys = _table_entries(h, player_tab)
    results: list[tuple[str, int]] = []
    seen: set[int] = set()
    for key, value_addr in player_keys:
        if _ru32(h, value_addr + 4) != _LJ_TTAB:
            continue
        ptr = _ru32(h, value_addr)
        if not ptr or not _is_heap(ptr) or ptr in seen:
            continue
        lower = key.lower()
        child_keys = _table_string_keys(h, ptr)
        looks_inventoryish = (
            any(token in lower for token in ("inven", "equip", "worn", "transmo", "stash", "object", "slot"))
            or any(token in ck.lower() for ck in child_keys for token in ("name", "slot", "type", "subtype", "encumber", "material"))
            or bool(_tab_get_ordered_tables(h, ptr))
        )
        if looks_inventoryish:
            results.append((key, ptr))
            seen.add(ptr)
    return results


def main() -> None:
    reader = MemoryReader()
    print("Attaching to t-engine.exe...")
    if not reader.attach():
        print("Failed to attach. Is the game running, with a character loaded, and this shell elevated?")
        sys.exit(1)

    h = reader._handle
    gt = reader._global_table
    game_tab = _tab_get_table(h, gt, "game")
    if game_tab is None:
        print("'game' not found in _G")
        sys.exit(1)
    player_tab = _tab_get_table(h, game_tab, "player")
    if player_tab is None:
        print("'player' not found in game")
        sys.exit(1)

    player_flat = _tab_dump_flat(h, player_tab)
    summary = {
        "player_ptr": f"0x{player_tab:08X}",
        "name": player_flat.get("name"),
        "level": player_flat.get("level"),
        "has_transmo": player_flat.get("has_transmo"),
        "top_level_keys_sample": _table_string_keys(h, player_tab)[:120],
        "candidate_subtables": [],
    }

    candidates = _candidate_subtables(h, player_tab)
    if not candidates:
        print("No obvious inventory/equipment/transmog candidate subtables found on game.player.")
    for key, ptr in candidates:
        summary["candidate_subtables"].append(_probe_container(h, ptr, key))

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    reader.detach()


if __name__ == "__main__":
    main()
