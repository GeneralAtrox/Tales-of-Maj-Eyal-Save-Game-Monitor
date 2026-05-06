from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from gui import memory_reader


class MemoryReaderValidationTests(unittest.TestCase):
    def test_is_gctab_rejects_uncommitted_or_unreadable_region(self) -> None:
        with (
            patch.object(memory_reader, "_is_committed_readable_address", return_value=False),
            patch.object(memory_reader, "_rpm") as rpm,
        ):
            self.assertFalse(memory_reader._is_gctab(1, 0x10000000))

        rpm.assert_not_called()

    def test_is_gctab_rejects_unreadable_or_wrong_type_memory(self) -> None:
        with (
            patch.object(memory_reader, "_is_committed_readable_address", return_value=True),
            patch.object(memory_reader, "_rpm", return_value=None),
        ):
            self.assertFalse(memory_reader._is_gctab(1, 0x10000000))

        wrong_type = bytearray(32)
        wrong_type[5] = 0x04  # GCstr, not GCtab
        with (
            patch.object(memory_reader, "_is_committed_readable_address", return_value=True),
            patch.object(memory_reader, "_rpm", return_value=bytes(wrong_type)),
        ):
            self.assertFalse(memory_reader._is_gctab(1, 0x10000000))

    def test_is_gctab_accepts_valid_table_header(self) -> None:
        raw = bytearray(32)
        raw[5] = memory_reader._GCT_TAB
        raw[8:12] = (0x20000000).to_bytes(4, "little")
        raw[20:24] = (0x21000000).to_bytes(4, "little")
        raw[28:32] = (0x7F).to_bytes(4, "little")

        with (
            patch.object(memory_reader, "_is_committed_readable_address", return_value=True),
            patch.object(memory_reader, "_rpm", return_value=bytes(raw)),
        ):
            self.assertTrue(memory_reader._is_gctab(1, 0x10000000))

    def test_validate_game_table_requires_plausible_singleton_key(self) -> None:
        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_find_strkey", return_value=None),
        ):
            self.assertFalse(memory_reader._validate_game_table(1, 0x10000000))

        def fake_find(_handle: int, _table: int, key: str) -> int | None:
            return 0x22000000 if key == "level" else None

        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_find_strkey", side_effect=fake_find),
        ):
            self.assertTrue(memory_reader._validate_game_table(1, 0x10000000))

    def test_validate_global_table_requires_valid_game_chain(self) -> None:
        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_get_table", return_value=0x20000000),
            patch.object(memory_reader, "_validate_game_table", return_value=True),
        ):
            self.assertEqual(memory_reader._validate_global_table(1, 0x10000000), 0x10000000)

        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_get_table", return_value=0x20000000),
            patch.object(memory_reader, "_validate_game_table", return_value=False),
        ):
            self.assertIsNone(memory_reader._validate_global_table(1, 0x10000000))

    def test_looks_like_lua_global_table_requires_standard_global_keys(self) -> None:
        found = {"_G", "_VERSION", "package", "string"}

        def fake_find(_handle: int, _table: int, key: str) -> int | None:
            return 0x22000000 if key in found else None

        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_find_strkey", side_effect=fake_find),
        ):
            self.assertTrue(memory_reader._looks_like_lua_global_table(1, 0x10000000))

        found.remove("string")
        with (
            patch.object(memory_reader, "_is_gctab", return_value=True),
            patch.object(memory_reader, "_tab_find_strkey", side_effect=fake_find),
        ):
            self.assertFalse(memory_reader._looks_like_lua_global_table(1, 0x10000000))

    def test_find_global_table_can_return_global_only_warmup_candidate(self) -> None:
        raw = bytearray(64)
        raw[5] = memory_reader._GCT_TAB
        raw[20:24] = (0x20000000).to_bytes(4, "little")
        raw[28:32] = (0x7F).to_bytes(4, "little")

        with (
            patch.object(memory_reader, "_iter_regions", return_value=[(0x10000000, bytes(raw))]),
            patch.object(memory_reader, "_tab_get_table", return_value=None),
            patch.object(memory_reader, "_tab_find_strkey", return_value=None),
            patch.object(memory_reader, "_looks_like_lua_global_table", return_value=True),
        ):
            self.assertIsNone(memory_reader._find_global_table(1))
            self.assertEqual(
                memory_reader._find_global_table(1, allow_global_only=True),
                0x10000000,
            )

    def test_iter_gctab_candidate_addresses_requires_alignment_and_shape(self) -> None:
        raw = bytearray(96)
        raw[6] = memory_reader._GCT_TAB  # unaligned table header candidate at off=1
        raw[37] = memory_reader._GCT_TAB  # aligned table header candidate at off=32
        raw[52:56] = (0x20000000).to_bytes(4, "little")
        raw[60:64] = (0x7F).to_bytes(4, "little")

        self.assertEqual(
            list(memory_reader._iter_gctab_candidate_addresses(0x10000000, bytes(raw))),
            [0x10000020],
        )

    def test_engine_armor_hardiness_applies_base_floor_and_bounds(self) -> None:
        self.assertEqual(memory_reader._engine_armor_hardiness(None), 30.0)
        self.assertEqual(memory_reader._engine_armor_hardiness(0.0), 30.0)
        self.assertEqual(memory_reader._engine_armor_hardiness(25.0), 55.0)
        self.assertEqual(memory_reader._engine_armor_hardiness(90.0), 100.0)
        self.assertEqual(memory_reader._engine_armor_hardiness(-40.0), 0.0)

    def test_engine_defense_uses_stats_and_precomputed_value(self) -> None:
        self.assertEqual(
            memory_reader._engine_defense(10.0, {"dex": 30.0, "lck": 55.0}),
            23,
        )
        self.assertEqual(memory_reader._engine_defense(10.0, {}, precomputed=42.0), 42.0)

    def test_tab_dump_stat_subtable_reads_integer_keyed_actor_stats(self) -> None:
        indexed_values = {
            3: 12.0,
            4: 20.0,
            5: 18.0,
            6: 16.0,
            7: 14.0,
            8: 50.0,
        }

        def fake_index(_handle: int, _table: int, idx: int) -> float | None:
            return indexed_values.get(idx)

        with (
            patch.object(memory_reader, "_tab_dump_flat", return_value={"stats.str": 30.0}),
            patch.object(memory_reader, "_tab_get_number_by_index", side_effect=fake_index),
        ):
            stats = memory_reader._tab_dump_stat_subtable(1, 0x20000000)

        self.assertEqual(
            stats,
            {
                "stats.str": 30.0,
                "stats.dex": 12.0,
                "stats.mag": 20.0,
                "stats.wil": 18.0,
                "stats.cun": 16.0,
                "stats.con": 14.0,
                "stats.lck": 50.0,
            },
        )

    def test_tab_dump_damage_subtable_reads_numeric_damage_type_keys(self) -> None:
        with (
            patch.object(memory_reader, "_tab_dump_flat", return_value={"inc_damage.all": 5.0}),
            patch.object(
                memory_reader,
                "_tab_dump_indexed_numbers",
                return_value={"inc_damage.FIRE": 25.0, "inc_damage.COLD": 10.0},
            ),
        ):
            values = memory_reader._tab_dump_damage_subtable(1, 0x20000000, prefix="inc_damage.")

        self.assertEqual(
            values,
            {
                "inc_damage.all": 5.0,
                "inc_damage.FIRE": 25.0,
                "inc_damage.COLD": 10.0,
            },
        )

    def test_tab_dump_indexed_numbers_reads_array_part_once(self) -> None:
        table_ptr = 0x10000000
        array_ptr = 0x20000000
        array = struct.pack("<d", 12.0) + struct.pack("<d", 25.0) + struct.pack("<d", 0.0)

        def fake_ru32(_handle: int, addr: int) -> int | None:
            if addr == table_ptr + 24:
                return 3
            if addr == table_ptr + 8:
                return array_ptr
            if addr == table_ptr + 20:
                return 0
            if addr == table_ptr + 28:
                return None
            return None

        with (
            patch.object(memory_reader, "_ru32", side_effect=fake_ru32),
            patch.object(memory_reader, "_is_heap", return_value=True),
            patch.object(memory_reader, "_rpm", return_value=array) as rpm,
        ):
            values = memory_reader._tab_dump_indexed_numbers(
                1,
                table_ptr,
                {1: "PHYSICAL", 2: "ARCANE", 3: "FIRE"},
            )

        self.assertEqual(values, {"PHYSICAL": 12.0, "ARCANE": 25.0, "FIRE": 0.0})
        rpm.assert_called_once_with(1, array_ptr, 24)

    def test_tab_has_any_entries_detects_array_values(self) -> None:
        table_ptr = 0x10000000
        array_ptr = 0x20000000
        array = (
            struct.pack("<II", 0, memory_reader._LJ_TNIL)
            + struct.pack("<II", 0x30000000, memory_reader._LJ_TTAB)
        )

        def fake_ru32(_handle: int, addr: int) -> int | None:
            if addr == table_ptr + 24:
                return 2
            if addr == table_ptr + 8:
                return array_ptr
            if addr == table_ptr + 20:
                return 0
            if addr == table_ptr + 28:
                return None
            return None

        with (
            patch.object(memory_reader, "_ru32", side_effect=fake_ru32),
            patch.object(memory_reader, "_is_heap", return_value=True),
            patch.object(memory_reader, "_rpm", return_value=array),
        ):
            self.assertTrue(memory_reader._tab_has_any_entries(1, table_ptr))

    def test_tab_dump_entity_snapshot_marks_unmodeled_combat_proc_hooks(self) -> None:
        actor_ptr = 0x10000000
        combat_ptr = 0x20000000
        hook_ptr = 0x30000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "combat":
                return combat_ptr
            if tab_ptr == combat_ptr and key == "special_on_hit":
                return hook_ptr
            return None

        with (
            patch.object(memory_reader, "_tab_dump_flat", return_value={}),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_has_any_entries", return_value=True),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot, {"combat.special_on_hit": True})

    def test_tab_dump_entity_snapshot_marks_equipped_weapon_proc_hooks(self) -> None:
        actor_ptr = 0x10000000
        inven_ptr = 0x20000000
        bucket_ptr = 0x30000000
        item_ptr = 0x40000000
        combat_ptr = 0x50000000
        hook_ptr = 0x60000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "inven":
                return inven_ptr
            if tab_ptr == item_ptr and key == "combat":
                return combat_ptr
            if tab_ptr == combat_ptr and key == "talent_on_hit":
                return hook_ptr
            return None

        def fake_ordered(_handle: int, tab_ptr: int) -> list[int]:
            if tab_ptr == inven_ptr:
                return [bucket_ptr]
            if tab_ptr == bucket_ptr:
                return [item_ptr]
            return []

        def fake_dump_flat(_handle: int, tab_ptr: int, *args: object, **kwargs: object) -> dict[str, str]:
            if tab_ptr == bucket_ptr:
                return {"short_name": "MAINHAND"}
            return {}

        with (
            patch.object(memory_reader, "_tab_dump_flat", side_effect=fake_dump_flat),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_get_ordered_tables", side_effect=fake_ordered),
            patch.object(memory_reader, "_tab_has_any_entries", return_value=True),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot, {"combat.talent_on_hit": True, "combat.source": "MAINHAND"})

    def test_tab_dump_entity_snapshot_prefers_primary_weapon_combat_fields(self) -> None:
        actor_ptr = 0x10000000
        actor_combat_ptr = 0x20000000
        actor_project_ptr = 0x21000000
        inven_ptr = 0x30000000
        bucket_ptr = 0x40000000
        item_ptr = 0x50000000
        weapon_combat_ptr = 0x60000000
        weapon_dammod_ptr = 0x61000000
        weapon_burst_ptr = 0x62000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "combat":
                return actor_combat_ptr
            if tab_ptr == actor_ptr and key == "inven":
                return inven_ptr
            if tab_ptr == actor_combat_ptr and key == "melee_project":
                return actor_project_ptr
            if tab_ptr == item_ptr and key == "combat":
                return weapon_combat_ptr
            if tab_ptr == weapon_combat_ptr and key == "dammod":
                return weapon_dammod_ptr
            if tab_ptr == weapon_combat_ptr and key == "burst_on_hit":
                return weapon_burst_ptr
            return None

        def fake_ordered(_handle: int, tab_ptr: int) -> list[int]:
            if tab_ptr == inven_ptr:
                return [bucket_ptr]
            if tab_ptr == bucket_ptr:
                return [item_ptr]
            return []

        def fake_dump_flat(
            _handle: int,
            tab_ptr: int,
            prefix: str = "",
            **_kwargs: object,
        ) -> dict[str, str | float]:
            if tab_ptr == actor_combat_ptr:
                return {"combat.dam": 10.0, "combat.atk": 3.0, "combat.damtype": "FIRE"}
            if tab_ptr == bucket_ptr:
                return {"short_name": "MAINHAND"}
            if tab_ptr == weapon_combat_ptr:
                return {"combat.dam": 50.0, "combat.apr": 7.0, "combat.damtype": "COLD"}
            if tab_ptr == weapon_dammod_ptr:
                return {"combat.dammod.str": 1.2}
            return {}

        def fake_damage_subtable(_handle: int, tab_ptr: int, prefix: str = "") -> dict[str, float]:
            if tab_ptr == actor_project_ptr:
                return {f"{prefix}FIRE": 5.0}
            if tab_ptr == weapon_burst_ptr:
                return {f"{prefix}COLD": 9.0}
            return {}

        with (
            patch.object(memory_reader, "_tab_dump_flat", side_effect=fake_dump_flat),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_get_ordered_tables", side_effect=fake_ordered),
            patch.object(memory_reader, "_tab_dump_damage_subtable", side_effect=fake_damage_subtable),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot["combat.dam"], 50.0)
        self.assertEqual(snapshot["combat.apr"], 7.0)
        self.assertEqual(snapshot["combat.damtype"], "COLD")
        self.assertEqual(snapshot["combat.dammod.str"], 1.2)
        self.assertEqual(snapshot["combat.burst_on_hit.COLD"], 9.0)
        self.assertEqual(snapshot["combat.source"], "MAINHAND")
        self.assertNotIn("combat.atk", snapshot)
        self.assertNotIn("combat.melee_project.FIRE", snapshot)

    def test_tab_dump_entity_snapshot_reads_offhand_weapon_combat_fields(self) -> None:
        actor_ptr = 0x10000000
        inven_ptr = 0x20000000
        bucket_ptr = 0x30000000
        item_ptr = 0x40000000
        offhand_combat_ptr = 0x50000000
        offhand_project_ptr = 0x51000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "inven":
                return inven_ptr
            if tab_ptr == item_ptr and key == "combat":
                return offhand_combat_ptr
            if tab_ptr == offhand_combat_ptr and key == "melee_project":
                return offhand_project_ptr
            return None

        def fake_ordered(_handle: int, tab_ptr: int) -> list[int]:
            if tab_ptr == inven_ptr:
                return [bucket_ptr]
            if tab_ptr == bucket_ptr:
                return [item_ptr]
            return []

        def fake_dump_flat(
            _handle: int,
            tab_ptr: int,
            prefix: str = "",
            **_kwargs: object,
        ) -> dict[str, str | float]:
            if tab_ptr == bucket_ptr:
                return {"short_name": "OFFHAND"}
            if tab_ptr == offhand_combat_ptr:
                return {"combat.dam": 30.0, "combat.apr": 4.0, "combat.damtype": "BLIGHT"}
            return {}

        def fake_damage_subtable(_handle: int, tab_ptr: int, prefix: str = "") -> dict[str, float]:
            if tab_ptr == offhand_project_ptr:
                return {f"{prefix}FIRE": 6.0}
            return {}

        with (
            patch.object(memory_reader, "_tab_dump_flat", side_effect=fake_dump_flat),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_get_ordered_tables", side_effect=fake_ordered),
            patch.object(memory_reader, "_tab_dump_damage_subtable", side_effect=fake_damage_subtable),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot["combat.offhand.dam"], 30.0)
        self.assertEqual(snapshot["combat.offhand.apr"], 4.0)
        self.assertEqual(snapshot["combat.offhand.damtype"], "BLIGHT")
        self.assertEqual(snapshot["combat.offhand.melee_project.FIRE"], 6.0)
        self.assertEqual(snapshot["combat.offhand.mult"], 0.5)
        self.assertEqual(snapshot["combat.offhand.source"], "OFFHAND")

    def test_tab_dump_entity_snapshot_reads_selected_effect_fields(self) -> None:
        actor_ptr = 0x10000000
        tmp_ptr = 0x20000000
        effect_ptr = 0x30000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "tmp":
                return tmp_ptr
            return None

        def fake_iter_string_entries(_handle: int, tab_ptr: int) -> list[tuple[str, int, int]]:
            if tab_ptr == tmp_ptr:
                return [
                    ("EFF_CURSE_OF_MADNESS", memory_reader._LJ_TTAB, effect_ptr),
                    ("EFF_UNRELATED", memory_reader._LJ_TTAB, 0x40000000),
                ]
            return []

        def fake_get_number(_handle: int, tab_ptr: int, key: str) -> float | None:
            if tab_ptr == effect_ptr and key == "level":
                return 5.0
            if tab_ptr == effect_ptr and key == "unlockLevel":
                return 1.0
            return None

        with (
            patch.object(memory_reader, "_tab_dump_flat", return_value={}),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_iter_string_entries", side_effect=fake_iter_string_entries),
            patch.object(memory_reader, "_tab_get_number", side_effect=fake_get_number),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot["effects.EFF_CURSE_OF_MADNESS.level"], 5.0)
        self.assertEqual(snapshot["effects.EFF_CURSE_OF_MADNESS.unlockLevel"], 1.0)
        self.assertNotIn("effects.EFF_UNRELATED.level", snapshot)

    def test_tab_dump_entity_snapshot_ignores_archery_mainhand_for_melee_combat(self) -> None:
        actor_ptr = 0x10000000
        actor_combat_ptr = 0x20000000
        inven_ptr = 0x30000000
        bucket_ptr = 0x40000000
        item_ptr = 0x50000000
        weapon_combat_ptr = 0x60000000

        def fake_get_table(_handle: int, tab_ptr: int, key: str) -> int | None:
            if tab_ptr == actor_ptr and key == "combat":
                return actor_combat_ptr
            if tab_ptr == actor_ptr and key == "inven":
                return inven_ptr
            if tab_ptr == item_ptr and key == "combat":
                return weapon_combat_ptr
            return None

        def fake_scalar(_handle: int, tab_ptr: int, key: str) -> str | None:
            if tab_ptr == item_ptr and key == "archery":
                return "bow"
            return None

        def fake_ordered(_handle: int, tab_ptr: int) -> list[int]:
            if tab_ptr == inven_ptr:
                return [bucket_ptr]
            if tab_ptr == bucket_ptr:
                return [item_ptr]
            return []

        def fake_dump_flat(
            _handle: int,
            tab_ptr: int,
            prefix: str = "",
            **_kwargs: object,
        ) -> dict[str, str | float]:
            if tab_ptr == actor_combat_ptr:
                return {"combat.dam": 10.0, "combat.damtype": "FIRE"}
            if tab_ptr == bucket_ptr:
                return {"short_name": "MAINHAND"}
            if tab_ptr == weapon_combat_ptr:
                return {"combat.dam": 50.0, "combat.damtype": "COLD"}
            return {}

        with (
            patch.object(memory_reader, "_tab_dump_flat", side_effect=fake_dump_flat),
            patch.object(memory_reader, "_tab_get_table", side_effect=fake_get_table),
            patch.object(memory_reader, "_tab_get_ordered_tables", side_effect=fake_ordered),
            patch.object(memory_reader, "_tab_get_scalar", side_effect=fake_scalar),
        ):
            snapshot = memory_reader._tab_dump_entity_snapshot(1, actor_ptr)

        self.assertEqual(snapshot["combat.dam"], 10.0)
        self.assertEqual(snapshot["combat.damtype"], "FIRE")
        self.assertNotIn("combat.source", snapshot)

    def test_ensure_game_table_skips_validation_until_interval_expires(self) -> None:
        reader = memory_reader.MemoryReader()
        reader._handle = 1
        reader._global_table = 0x10000000
        reader._game_table = 0x20000000
        reader._game_table_reads_until_validate = 2

        with patch.object(memory_reader, "_validate_game_table") as validate:
            self.assertEqual(reader._ensure_game_table(), 0x20000000)
            self.assertEqual(reader._ensure_game_table(), 0x20000000)

        validate.assert_not_called()
        self.assertEqual(reader._game_table_reads_until_validate, 0)

    def test_ensure_game_table_revalidates_when_interval_expires(self) -> None:
        reader = memory_reader.MemoryReader()
        reader._handle = 1
        reader._global_table = 0x10000000
        reader._game_table = 0x20000000
        reader._game_table_reads_until_validate = 0

        with patch.object(memory_reader, "_validate_game_table", return_value=True) as validate:
            self.assertEqual(reader._ensure_game_table(), 0x20000000)

        validate.assert_called_once_with(1, 0x20000000)
        self.assertEqual(
            reader._game_table_reads_until_validate,
            memory_reader._GAME_TABLE_REVALIDATE_INTERVAL,
        )

    def test_ensure_game_table_rediscover_after_stale_cached_table(self) -> None:
        reader = memory_reader.MemoryReader()
        reader._handle = 1
        reader._global_table = 0x10000000
        reader._game_table = 0x20000000
        reader._player_table = 0x30000000
        reader._game_table_reads_until_validate = 0

        with (
            patch.object(memory_reader, "_validate_game_table", side_effect=[False, True]),
            patch.object(memory_reader, "_tab_get_table", return_value=0x21000000),
        ):
            self.assertEqual(reader._ensure_game_table(), 0x21000000)

        self.assertEqual(reader._game_table, 0x21000000)
        self.assertEqual(reader._player_table, 0)
        self.assertEqual(
            reader._game_table_reads_until_validate,
            memory_reader._GAME_TABLE_REVALIDATE_INTERVAL,
        )


if __name__ == "__main__":
    unittest.main()
