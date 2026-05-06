from __future__ import annotations

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
