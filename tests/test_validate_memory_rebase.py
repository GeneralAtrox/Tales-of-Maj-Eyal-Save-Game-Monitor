from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

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
            gctab_ok=True,
        )

        self.assertTrue(row["safe_to_read"])
        self.assertTrue(row["gctab_ok"])
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
            gctab_ok=True,
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
            gctab_ok=False,
        )

        self.assertFalse(guarded["safe_to_read"])
        self.assertFalse(out_of_range["safe_to_read"])
        self.assertFalse(out_of_range["gctab_ok"])

    def test_cheat_table_report_validates_aobscanmodule_and_symbol_policy(self) -> None:
        data = _minimal_pe32()
        data[0x210 : 0x213] = bytes.fromhex("AA BB CC")
        pe = rebase._parse_pe(bytes(data))
        ct_text = """<?xml version="1.0"?>
<CheatTable>
  <AssemblerScript>
    aobscanmodule(tome_23,t-engine.exe,AA BB CC)
    alloc(tome_11,2048)
    label(tome_13)
    registersymbol(tome_23)
    registersymbol(tome_13)
  </AssemblerScript>
  <Address>tome_13</Address>
</CheatTable>
"""
        with tempfile.TemporaryDirectory() as tmp:
            ct_path = Path(tmp) / "sample.CT"
            ct_path.write_text(ct_text, encoding="utf-8")

            report = rebase._cheat_table_report(ct_path, bytes(data), pe, None, None)

        assert report is not None
        self.assertEqual(report["aobscanmodule_count"], 1)
        [aob] = report["aobscanmodules"]
        self.assertEqual(aob["symbol"], "tome_23")
        self.assertEqual(aob["status"], "OK")
        self.assertEqual(aob["match_count"], 1)
        self.assertEqual(aob["matches"][0]["rva"], 0x1010)
        policies = {row["name"]: row for row in report["registered_symbols"]}
        self.assertTrue(policies["tome_23"]["rebasable"])
        self.assertFalse(policies["tome_13"]["rebasable"])
        self.assertEqual(policies["tome_13"]["address_references"], 1)

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

    def test_compare_baseline_reports_cheat_table_changes(self) -> None:
        baseline = {
            "executable": {
                "size": 100,
                "sha256": "same",
                "machine": 0x014C,
                "time_date_stamp": 1,
                "pe_image_size": 0x3000,
            },
            "signatures": {},
            "luajit_layout": {"gctab_size": 32},
            "cheat_engine_table": {
                "sha256": "old",
                "aobscanmodule_count": 1,
                "address_symbols": {"tome_13": 1},
                "aobscanmodules": [
                    {
                        "symbol": "tome_23",
                        "module": "t-engine.exe",
                        "pattern": "AA BB",
                        "status": "OK",
                        "match_count": 1,
                        "matches": [{"rva": 0x1000}],
                    }
                ],
            },
        }
        report = {
            **baseline,
            "cheat_engine_table": {
                "sha256": "new",
                "aobscanmodule_count": 1,
                "address_symbols": {"tome_13": 1},
                "aobscanmodules": [
                    {
                        "symbol": "tome_23",
                        "module": "t-engine.exe",
                        "pattern": "AA CC",
                        "status": "OK",
                        "match_count": 1,
                        "matches": [{"rva": 0x1010}],
                    }
                ],
            },
        }

        diffs = rebase._compare_with_baseline(report, baseline)

        self.assertIn("cheat_engine_table.sha256: baseline='old' current='new'", diffs)
        self.assertIn(
            "cheat_engine_table.aobscanmodules[0].pattern: baseline='AA BB' current='AA CC'",
            diffs,
        )
        self.assertIn("cheat_engine_table.aobscanmodules[0].rvas: baseline=[4096] current=[4112]", diffs)


if __name__ == "__main__":
    unittest.main()
