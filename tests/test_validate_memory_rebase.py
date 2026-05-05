from __future__ import annotations

import struct
import unittest

from tools import validate_memory_rebase as rebase


def _minimal_pe32() -> bytearray:
    data = bytearray(0x500)
    data[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"

    # COFF header
    struct.pack_into("<H", data, pe_offset + 4, 0x014C)  # IMAGE_FILE_MACHINE_I386
    struct.pack_into("<H", data, pe_offset + 6, 1)  # one section
    struct.pack_into("<I", data, pe_offset + 8, 0x12345678)
    struct.pack_into("<H", data, pe_offset + 20, 0xE0)  # PE32 optional header size

    optional_offset = pe_offset + 24
    struct.pack_into("<H", data, optional_offset, 0x10B)  # PE32
    struct.pack_into("<I", data, optional_offset + 28, 0x00400000)
    struct.pack_into("<I", data, optional_offset + 56, 0x3000)

    section_offset = optional_offset + 0xE0
    data[section_offset : section_offset + 8] = b".text\0\0\0"
    struct.pack_into("<I", data, section_offset + 8, 0x200)  # virtual size
    struct.pack_into("<I", data, section_offset + 12, 0x1000)  # RVA
    struct.pack_into("<I", data, section_offset + 16, 0x200)  # raw size
    struct.pack_into("<I", data, section_offset + 20, 0x200)  # raw pointer
    return data


class MemoryRebaseValidatorTests(unittest.TestCase):
    def test_find_pattern_supports_wildcards_and_multiple_matches(self) -> None:
        pattern = rebase._parse_pattern("AA ?? 0f")
        self.assertEqual(pattern, (0xAA, None, 0x0F))
        self.assertEqual(rebase._find_pattern(bytes.fromhex("AA 11 0F AA 22 0F"), pattern), [0, 3])

    def test_parse_pe32_and_map_file_offset_to_rva(self) -> None:
        pe = rebase._parse_pe(bytes(_minimal_pe32()))

        self.assertEqual(pe.machine, 0x014C)
        self.assertEqual(pe.time_date_stamp, 0x12345678)
        self.assertEqual(pe.image_base, 0x00400000)
        self.assertEqual(pe.size_of_image, 0x3000)
        self.assertEqual(len(pe.sections), 1)

        self.assertEqual(rebase._file_offset_to_rva(pe, 0x210), (0x1010, ".text"))
        self.assertIsNone(rebase._file_offset_to_rva(pe, 0x50))

    def test_classify_address_reports_safe_private_readwrite_region(self) -> None:
        row = rebase._classify_address(
            0x20000020,
            rebase.MemoryRegion(
                base=0x20000000,
                size=0x1000,
                state=rebase.MEM_COMMIT,
                protect=rebase.PAGE_READWRITE,
                type=rebase.MEM_PRIVATE,
            ),
            size=32,
            reusable_as="resolve from _G.game",
            rebasable=False,
        )

        self.assertTrue(row["safe_to_read"])
        self.assertTrue(row["readable"])
        self.assertTrue(row["writable"])
        self.assertFalse(row["executable"])
        self.assertFalse(row["rebasable"])
        self.assertEqual(row["type_label"], "MEM_PRIVATE")
        self.assertEqual(row["protect_label"], "PAGE_READWRITE")

    def test_classify_address_rejects_guarded_or_out_of_range_region(self) -> None:
        guarded = rebase._classify_address(
            0x20000020,
            rebase.MemoryRegion(
                base=0x20000000,
                size=0x1000,
                state=rebase.MEM_COMMIT,
                protect=rebase.PAGE_READWRITE | rebase.PAGE_GUARD,
                type=rebase.MEM_PRIVATE,
            ),
            size=32,
            reusable_as="current process only",
            rebasable=False,
        )
        out_of_range = rebase._classify_address(
            0x20000FF0,
            rebase.MemoryRegion(
                base=0x20000000,
                size=0x1000,
                state=rebase.MEM_COMMIT,
                protect=rebase.PAGE_READWRITE,
                type=rebase.MEM_PRIVATE,
            ),
            size=32,
            reusable_as="current process only",
            rebasable=False,
        )

        self.assertFalse(guarded["safe_to_read"])
        self.assertFalse(out_of_range["safe_to_read"])

    def test_compare_baseline_reports_executable_and_rva_changes(self) -> None:
        baseline = {
            "executable": {
                "size": 100,
                "sha256": "old",
                "machine": 0x014C,
                "time_date_stamp": 1,
                "pe_image_size": 0x3000,
            },
            "signatures": {
                "anchor": {
                    "pattern": "AA BB",
                    "status": "OK",
                    "match_count": 1,
                    "matches": [{"rva": 0x1000}],
                }
            },
            "luajit_layout": {"runtime": "LuaJIT 2.0.2"},
        }
        report = {
            "executable": {
                "size": 100,
                "sha256": "new",
                "machine": 0x014C,
                "time_date_stamp": 2,
                "pe_image_size": 0x3000,
            },
            "signatures": {
                "anchor": {
                    "pattern": "AA BB",
                    "status": "OK",
                    "match_count": 1,
                    "matches": [{"rva": 0x1010}],
                }
            },
            "luajit_layout": {"runtime": "LuaJIT 2.0.2"},
        }

        diffs = rebase._compare_with_baseline(report, baseline)

        self.assertIn("executable.sha256: baseline='old' current='new'", diffs)
        self.assertIn("executable.time_date_stamp: baseline=1 current=2", diffs)
        self.assertIn("anchor.rvas: baseline=[4096] current=[4112]", diffs)

    def test_compare_baseline_accepts_identical_report(self) -> None:
        report = {
            "executable": {
                "size": 100,
                "sha256": "same",
                "machine": 0x014C,
                "time_date_stamp": 1,
                "pe_image_size": 0x3000,
            },
            "signatures": {
                "anchor": {
                    "pattern": "AA BB",
                    "status": "OK",
                    "match_count": 1,
                    "matches": [{"rva": 0x1000}],
                }
            },
            "luajit_layout": {"runtime": "LuaJIT 2.0.2"},
        }

        self.assertEqual(rebase._compare_with_baseline(report, report), [])

    def test_compare_baseline_reports_luajit_layout_changes(self) -> None:
        report = {
            "executable": {
                "size": 100,
                "sha256": "same",
                "machine": 0x014C,
                "time_date_stamp": 1,
                "pe_image_size": 0x3000,
            },
            "signatures": {},
            "luajit_layout": {"gctab_size": 32},
        }
        baseline = {
            "executable": {
                "size": 100,
                "sha256": "same",
                "machine": 0x014C,
                "time_date_stamp": 1,
                "pe_image_size": 0x3000,
            },
            "signatures": {},
            "luajit_layout": {"gctab_size": 40},
        }

        self.assertEqual(
            rebase._compare_with_baseline(report, baseline),
            ["luajit_layout: baseline does not match current reader assumptions"],
        )


if __name__ == "__main__":
    unittest.main()
