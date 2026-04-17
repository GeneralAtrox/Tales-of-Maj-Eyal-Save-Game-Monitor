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
        "__transmo",
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


def _bucket_items(h: int, bucket_ptr: int) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for idx, item_ptr in enumerate(_tab_get_ordered_tables(h, bucket_ptr)[:8], start=1):
        items.append(
            {
                "index": idx,
                "ptr": f"0x{item_ptr:08X}",
                "keys": _table_string_keys(h, item_ptr)[:24],
                "item_like": _item_like_snapshot(h, item_ptr),
                "transmo_fields": {
                    key: _value_preview(h, value_addr)
                    for key, value_addr in _table_entries(h, item_ptr)
                    if "transmo" in key.lower() or "chest" in key.lower()
                },
            }
        )
    return items


def _probe_inven_bucket(h: int, bucket_ptr: int) -> dict[str, object]:
    flat = _tab_dump_flat(h, bucket_ptr)
    return {
        "ptr": f"0x{bucket_ptr:08X}",
        "name": flat.get("name"),
        "short_name": flat.get("short_name"),
        "id": flat.get("id"),
        "max": flat.get("max"),
        "worn": flat.get("worn"),
        "item_count": len(_tab_get_ordered_tables(h, bucket_ptr)),
        "items": _bucket_items(h, bucket_ptr),
    }


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


def _find_transmo_chest_entity(h: int, game_tab: int) -> int | None:
    entities_tab = _tab_get_table(h, game_tab, "entities")
    if entities_tab is None:
        level_tab = _tab_get_table(h, game_tab, "level")
        if level_tab is not None:
            entities_tab = _tab_get_table(h, level_tab, "entities")
    if entities_tab is None:
        return None
    for ent_ptr in _tab_iter_table_values(h, entities_tab):
        flat = _tab_dump_flat(h, ent_ptr)
        if str(flat.get("define_as") or "") == "TRANSMO_CHEST":
            return ent_ptr
    return None


def _probe_named_children(h: int, tab_ptr: int) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for key, value_addr in _table_entries(h, tab_ptr):
        if _ru32(h, value_addr + 4) != _LJ_TTAB:
            continue
        child_ptr = _ru32(h, value_addr)
        if not child_ptr or not _is_heap(child_ptr):
            continue
        out.append(
            {
                "key": key,
                "ptr": f"0x{child_ptr:08X}",
                "asize": _tab_array_size(h, child_ptr),
                "hmask": _tab_hmask(h, child_ptr),
                "string_keys": _table_string_keys(h, child_ptr)[:24],
                "ordered_table_count": len(_tab_get_ordered_tables(h, child_ptr)),
                "table_value_count": len(_tab_iter_table_values(h, child_ptr)),
                "sample_item_like": _item_like_snapshot(h, _tab_get_ordered_tables(h, child_ptr)[0])
                if _tab_get_ordered_tables(h, child_ptr)
                else {},
            }
        )
    return out


def _find_pointer_references(
    h: int,
    tab_ptr: int,
    *,
    target_ptr: int,
    path: str,
    max_depth: int,
    seen: set[int] | None = None,
) -> list[dict[str, object]]:
    if seen is None:
        seen = set()
    if tab_ptr in seen or max_depth < 0:
        return []
    seen.add(tab_ptr)

    matches: list[dict[str, object]] = []
    for key, value_addr in _table_entries(h, tab_ptr):
        itype = _ru32(h, value_addr + 4)
        val_ptr = _ru32(h, value_addr)
        next_path = f"{path}.{key}"
        if itype == _LJ_TTAB and val_ptr == target_ptr:
            matches.append({"path": next_path, "kind": "table_ref"})
        if max_depth == 0 or itype != _LJ_TTAB or not val_ptr or not _is_heap(val_ptr):
            continue
        matches.extend(
            _find_pointer_references(
                h,
                val_ptr,
                target_ptr=target_ptr,
                path=next_path,
                max_depth=max_depth - 1,
                seen=seen,
            )
        )
    return matches


def _probe_child_tables_deep(h: int, tab_ptr: int, *, depth: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for child in _probe_named_children(h, tab_ptr):
        rows.append(child)
        if depth <= 0:
            continue
        child_ptr_text = child.get("ptr")
        if not isinstance(child_ptr_text, str):
            continue
        child_ptr = int(child_ptr_text, 16)
        grand = _probe_named_children(h, child_ptr)
        if grand:
            child["children"] = grand[:20]
    return rows


def _probe_table_summary(h: int, tab_ptr: int) -> dict[str, object]:
    ordered = _tab_get_ordered_tables(h, tab_ptr)
    table_values = _tab_iter_table_values(h, tab_ptr)
    return {
        "ptr": f"0x{tab_ptr:08X}",
        "asize": _tab_array_size(h, tab_ptr),
        "hmask": _tab_hmask(h, tab_ptr),
        "string_keys": _table_string_keys(h, tab_ptr)[:30],
        "ordered_table_count": len(ordered),
        "table_value_count": len(table_values),
        "ordered_item_samples": [
            {
                "index": idx,
                "ptr": f"0x{sub_ptr:08X}",
                "keys": _table_string_keys(h, sub_ptr)[:24],
                "item_like": _item_like_snapshot(h, sub_ptr),
            }
            for idx, sub_ptr in enumerate(ordered[:8], start=1)
        ],
        "table_value_samples": [
            {
                "index": idx,
                "ptr": f"0x{sub_ptr:08X}",
                "keys": _table_string_keys(h, sub_ptr)[:24],
                "item_like": _item_like_snapshot(h, sub_ptr),
            }
            for idx, sub_ptr in enumerate(table_values[:8], start=1)
        ],
    }


def _find_matching_paths(
    h: int,
    tab_ptr: int,
    *,
    path: str,
    max_depth: int,
    terms: tuple[str, ...],
    seen: set[int] | None = None,
) -> list[dict[str, object]]:
    if seen is None:
        seen = set()
    if tab_ptr in seen or max_depth < 0:
        return []
    seen.add(tab_ptr)

    matches: list[dict[str, object]] = []
    for key, value_addr in _table_entries(h, tab_ptr):
        key_l = key.lower()
        value_preview = _value_preview(h, value_addr)
        if any(term in key_l or term in value_preview.lower() for term in terms):
            matches.append(
                {
                    "path": f"{path}.{key}",
                    "value": value_preview,
                }
            )
        if max_depth == 0:
            continue
        if _ru32(h, value_addr + 4) != _LJ_TTAB:
            continue
        child_ptr = _ru32(h, value_addr)
        if not child_ptr or not _is_heap(child_ptr):
            continue
        matches.extend(
            _find_matching_paths(
                h,
                child_ptr,
                path=f"{path}.{key}",
                max_depth=max_depth - 1,
                terms=terms,
                seen=seen,
            )
        )
    return matches


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
        "inventory_buckets": [],
        "transmo_matches": [],
        "game_transmo_matches": [],
        "transmo_chest_entity": None,
        "transmo_chest_refs": [],
        "transmo_chest_followups": {},
        "candidate_subtables": [],
    }

    inven_tab = _tab_get_table(h, player_tab, "inven")
    if inven_tab is not None:
        summary["inventory_buckets"] = [
            _probe_inven_bucket(h, bucket_ptr)
            for bucket_ptr in _tab_get_ordered_tables(h, inven_tab)
        ]

    summary["transmo_matches"] = _find_matching_paths(
        h,
        player_tab,
        path="player",
        max_depth=4,
        terms=("transmo", "transmog", "transmogr", "chest"),
    )[:80]
    summary["game_transmo_matches"] = _find_matching_paths(
        h,
        game_tab,
        path="game",
        max_depth=4,
        terms=("transmo", "transmog", "transmogr", "chest"),
    )[:120]

    chest_ptr = _find_transmo_chest_entity(h, game_tab)
    if chest_ptr is not None:
        chest_flat = _tab_dump_flat(h, chest_ptr)
        chest_entries = {key: value_addr for key, value_addr in _table_entries(h, chest_ptr)}
        summary["transmo_chest_entity"] = {
            "ptr": f"0x{chest_ptr:08X}",
            "fields": {
                key: chest_flat.get(key)
                for key in ("name", "define_as", "type", "subtype", "image", "desc")
                if key in chest_flat
            },
            "child_tables": _probe_child_tables_deep(h, chest_ptr, depth=1),
        }
        summary["transmo_chest_refs"] = _find_pointer_references(
            h,
            game_tab,
            target_ptr=chest_ptr,
            path="game",
            max_depth=5,
        )[:120]
        for key in ("in_inven", "carrier", "carried", "use_power"):
            value_addr = chest_entries.get(key)
            if value_addr is None or _ru32(h, value_addr + 4) != _LJ_TTAB:
                continue
            child_ptr = _ru32(h, value_addr)
            if child_ptr and _is_heap(child_ptr):
                summary["transmo_chest_followups"][key] = _probe_table_summary(h, child_ptr)

        object_talent_data = _tab_get_table(h, player_tab, "object_talent_data")
        if object_talent_data is not None:
            obj_matches: list[dict[str, object]] = []
            for key, value_addr in _table_entries(h, object_talent_data):
                if _ru32(h, value_addr + 4) != _LJ_TTAB:
                    continue
                child_ptr = _ru32(h, value_addr)
                if not child_ptr or not _is_heap(child_ptr):
                    continue
                obj_tab = _tab_get_table(h, child_ptr, "obj")
                if obj_tab == chest_ptr:
                    obj_matches.append(
                        {
                            "path": f"player.object_talent_data.{key}.obj",
                            "holder_ptr": f"0x{child_ptr:08X}",
                            "holder_keys": _table_string_keys(h, child_ptr)[:20],
                            "holder_summary": _probe_table_summary(h, child_ptr),
                        }
                    )
            if obj_matches:
                summary["transmo_chest_followups"]["object_talent_data_refs"] = obj_matches[:20]

    candidates = _candidate_subtables(h, player_tab)
    if not candidates:
        print("No obvious inventory/equipment/transmog candidate subtables found on game.player.")
    for key, ptr in candidates:
        summary["candidate_subtables"].append(_probe_container(h, ptr, key))

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    reader.detach()


if __name__ == "__main__":
    main()
