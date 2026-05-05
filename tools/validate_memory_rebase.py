"""
validate_memory_rebase.py
-------------------------
Validate rebasable t-engine.exe anchors and classify live Lua heap roots.

This is intentionally read-only. Module addresses are reported as
``Module.Base + RVA``. Lua GC object addresses are reported as current-session
only and must be rediscovered through the table chain.

Run:
    python tools/validate_memory_rebase.py
    python tools/validate_memory_rebase.py --exe C:\\path\\to\\t-engine.exe
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import copy
import hashlib
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_BASELINE_PATH = PROJECT_ROOT / "docs" / "memory_rebase_baseline.json"
DEFAULT_CT_PATH = PROJECT_ROOT / "Tales of Maj'Eyal_v3.CT"


DEFAULT_EXE_PATHS: tuple[Path, ...] = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\TalesMajEyal\t-engine.exe"),
    Path(r"C:\Program Files\Steam\steamapps\common\TalesMajEyal\t-engine.exe"),
)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
_READABLE_PAGE_PROTECTIONS = {
    PAGE_READONLY,
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
}
_WRITABLE_PAGE_PROTECTIONS = {
    PAGE_READWRITE,
    PAGE_WRITECOPY,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
}
_EXECUTABLE_PAGE_PROTECTIONS = {
    PAGE_EXECUTE,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
}
_AOBSCANMODULE_RE = re.compile(
    r"\baobscanmodule\s*\(\s*([^,\s]+)\s*,\s*([^,\s]+)\s*,\s*([^)]+?)\s*\)",
    re.IGNORECASE,
)
_ALLOC_RE = re.compile(r"\balloc\s*\(\s*([^,\s)]+)", re.IGNORECASE)
_LABEL_RE = re.compile(r"\blabel\s*\(\s*([^,\s)]+)", re.IGNORECASE)
_REGISTER_SYMBOL_RE = re.compile(r"\bregistersymbol\s*\(\s*([^,\s)]+)", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"<Address>([^<]+)</Address>", re.IGNORECASE)

LUAJIT_LAYOUT: dict[str, object] = {
    "runtime": "LuaJIT 2.0.2",
    "target": "x86",
    "gc64": False,
    "tvalue_size": 8,
    "node_size": 24,
    "gctab_size": 32,
    "gc_offsets": {
        "gct": 5,
    },
    "gcstr_offsets": {
        "len": 12,
        "data": 16,
    },
    "gctab_offsets": {
        "array": 8,
        "node": 20,
        "asize": 24,
        "hmask": 28,
    },
    "node_offsets": {
        "val": 0,
        "key": 8,
    },
    "tvalue_tags": {
        "LJ_TSTR": 0xFFFFFFFB,
        "LJ_TTAB": 0xFFFFFFF4,
        "LJ_TNUMX": 0xFFFFFFF2,
        "LJ_TTRUE": 0xFFFFFFFD,
        "LJ_TFALSE": 0xFFFFFFFE,
        "LJ_TNIL": 0xFFFFFFFF,
    },
}


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_pointer: int
    raw_size: int


@dataclass(frozen=True, slots=True)
class PeImage:
    machine: int
    time_date_stamp: int
    image_base: int
    size_of_image: int
    sections: tuple[Section, ...]


@dataclass(frozen=True, slots=True)
class MemoryRegion:
    base: int
    size: int
    state: int
    protect: int
    type: int


@dataclass(frozen=True, slots=True)
class MemorySignature:
    name: str
    pattern: str
    purpose: str
    reusable_as: str
    expected_unique: bool = True


SIGNATURES: tuple[MemorySignature, ...] = (
    MemorySignature(
        name="ce_tvalue_nil_branch_context",
        pattern="83 79 04 FF 74 36 0F B6 46 FD 8B 29 8B 49 04 89",
        purpose="Cheat Engine TValue/key-walk hook context. Not used by the Python GUI.",
        reusable_as="AOB -> RVA -> Module.Base + RVA",
    ),
    MemorySignature(
        name="ce_tvalue_nil_branch_short",
        pattern="83 79 04 FF 74 36",
        purpose="Exact AOB used by Tales of Maj'Eyal_v3.CT.",
        reusable_as="AOB -> RVA -> Module.Base + RVA",
    ),
    MemorySignature(
        name="luajit_version_string",
        pattern="4C 75 61 4A 49 54 20 32 2E 30 2E 32",
        purpose="LuaJIT version fingerprint, useful for rejecting changed runtime layouts.",
        reusable_as="AOB/data RVA fingerprint only",
    ),
)


def _parse_pattern(pattern: str) -> tuple[int | None, ...]:
    out: list[int | None] = []
    for token in pattern.split():
        if token in {"?", "??"}:
            out.append(None)
        else:
            out.append(int(token, 16))
    return tuple(out)


def _find_pattern(data: bytes, pattern: tuple[int | None, ...]) -> list[int]:
    if not pattern:
        return []
    first_index = next((index for index, byte in enumerate(pattern) if byte is not None), None)
    if first_index is None:
        return []
    first_byte = pattern[first_index]
    assert first_byte is not None

    matches: list[int] = []
    start = 0
    limit = len(data) - len(pattern)
    while start <= limit:
        found = data.find(bytes((first_byte,)), start + first_index)
        if found < 0:
            break
        candidate = found - first_index
        if candidate < 0:
            start = found + 1
            continue
        for offset, expected in enumerate(pattern):
            if expected is not None and data[candidate + offset] != expected:
                break
        else:
            matches.append(candidate)
        start = candidate + 1
    return matches


def _parse_pe(data: bytes) -> PeImage:
    if len(data) < 0x100 or data[:2] != b"MZ":
        raise ValueError("not a PE/MZ executable")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("missing PE signature")

    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    section_count = struct.unpack_from("<H", data, pe_offset + 6)[0]
    time_date_stamp = struct.unpack_from("<I", data, pe_offset + 8)[0]
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    magic = struct.unpack_from("<H", data, optional_offset)[0]
    if magic == 0x10B:  # PE32
        image_base = struct.unpack_from("<I", data, optional_offset + 28)[0]
    elif magic == 0x20B:  # PE32+
        image_base = struct.unpack_from("<Q", data, optional_offset + 24)[0]
    else:
        raise ValueError(f"unsupported PE optional header magic 0x{magic:X}")
    size_of_image = struct.unpack_from("<I", data, optional_offset + 56)[0]

    sections: list[Section] = []
    section_offset = optional_offset + optional_size
    for index in range(section_count):
        offset = section_offset + index * 40
        name = data[offset : offset + 8].rstrip(b"\0").decode("ascii", errors="replace")
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, offset + 8)
        sections.append(
            Section(
                name=name,
                virtual_address=virtual_address,
                virtual_size=virtual_size,
                raw_pointer=raw_pointer,
                raw_size=raw_size,
            )
        )
    return PeImage(
        machine=machine,
        time_date_stamp=time_date_stamp,
        image_base=image_base,
        size_of_image=size_of_image,
        sections=tuple(sections),
    )


def _file_offset_to_rva(pe: PeImage, file_offset: int) -> tuple[int, str] | None:
    for section in pe.sections:
        if section.raw_pointer <= file_offset < section.raw_pointer + section.raw_size:
            return section.virtual_address + (file_offset - section.raw_pointer), section.name
    return None


def _pattern_match_rows(
    data: bytes,
    pattern: tuple[int | None, ...],
    pe: PeImage,
    live_module: tuple[int, int] | None,
    running: tuple[int, Path] | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file_offset in _find_pattern(data, pattern):
        mapped = _file_offset_to_rva(pe, file_offset)
        row: dict[str, object] = {"file_offset": file_offset}
        if mapped is not None:
            rva, section = mapped
            row.update({"rva": rva, "section": section})
            if live_module and running:
                live_addr = live_module[0] + rva
                live_bytes = _read_process_bytes(running[0], live_addr, len(pattern))
                live_ok = live_bytes is not None and all(
                    expected is None or live_bytes[index] == expected
                    for index, expected in enumerate(pattern)
                )
                row.update({"live_addr": live_addr, "live_verified": live_ok})
        rows.append(row)
    return rows


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_exe() -> Path | None:
    for path in DEFAULT_EXE_PATHS:
        if path.exists():
            return path
    return None


def _iter_process_ids() -> list[int]:
    psapi = ctypes.WinDLL("Psapi.dll")
    count = 256
    while True:
        buffer = (ctypes.wintypes.DWORD * count)()
        needed = ctypes.wintypes.DWORD()
        if not psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return []
        returned = needed.value // ctypes.sizeof(ctypes.wintypes.DWORD)
        if returned < count:
            return [int(buffer[index]) for index in range(returned) if buffer[index]]
        count *= 2


def _process_image_name(pid: int) -> str | None:
    k32 = ctypes.windll.kernel32
    process = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        size = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        k32.CloseHandle(process)


def _find_running_t_engine() -> tuple[int, Path] | None:
    for pid in _iter_process_ids():
        image_name = _process_image_name(pid)
        if image_name and Path(image_name).name.lower() == "t-engine.exe":
            return pid, Path(image_name)
    return None


def _module_base(pid: int, image_path: Path) -> tuple[int, int] | None:
    k32 = ctypes.windll.kernel32
    psapi = ctypes.WinDLL("Psapi.dll")

    process = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not process:
        return None
    try:
        modules = (ctypes.c_void_p * 1024)()
        needed = ctypes.wintypes.DWORD()
        if not psapi.EnumProcessModules(process, ctypes.byref(modules), ctypes.sizeof(modules), ctypes.byref(needed)):
            return None
        count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(modules))

        class ModuleInfo(ctypes.Structure):
            _fields_ = [
                ("lpBaseOfDll", ctypes.c_void_p),
                ("SizeOfImage", ctypes.wintypes.DWORD),
                ("EntryPoint", ctypes.c_void_p),
            ]

        for index in range(count):
            module = modules[index]
            buffer = ctypes.create_unicode_buffer(32768)
            psapi.GetModuleFileNameExW(process, module, buffer, 32768)
            if Path(buffer.value).resolve() != image_path.resolve():
                continue
            info = ModuleInfo()
            if not psapi.GetModuleInformation(process, module, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            return int(info.lpBaseOfDll or 0), int(info.SizeOfImage)
    finally:
        k32.CloseHandle(process)
    return None


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.wintypes.DWORD),
        ("Protect", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
    ]


def _state_label(state: int) -> str:
    return "MEM_COMMIT" if state == MEM_COMMIT else f"0x{state:X}"


def _type_label(memory_type: int) -> str:
    labels = {
        MEM_IMAGE: "MEM_IMAGE",
        MEM_MAPPED: "MEM_MAPPED",
        MEM_PRIVATE: "MEM_PRIVATE",
    }
    return labels.get(memory_type, f"0x{memory_type:X}")


def _protect_label(protect: int) -> str:
    access = protect & 0xFF
    labels = {
        PAGE_NOACCESS: "PAGE_NOACCESS",
        PAGE_READONLY: "PAGE_READONLY",
        PAGE_READWRITE: "PAGE_READWRITE",
        PAGE_WRITECOPY: "PAGE_WRITECOPY",
        PAGE_EXECUTE: "PAGE_EXECUTE",
        PAGE_EXECUTE_READ: "PAGE_EXECUTE_READ",
        PAGE_EXECUTE_READWRITE: "PAGE_EXECUTE_READWRITE",
        PAGE_EXECUTE_WRITECOPY: "PAGE_EXECUTE_WRITECOPY",
    }
    parts = [labels.get(access, f"0x{access:X}")]
    if protect & PAGE_GUARD:
        parts.append("PAGE_GUARD")
    return "|".join(parts)


def _query_memory_region(process: int, addr: int) -> MemoryRegion | None:
    mbi = _MBI()
    ret = ctypes.windll.kernel32.VirtualQueryEx(
        process,
        ctypes.c_void_p(addr),
        ctypes.byref(mbi),
        ctypes.sizeof(mbi),
    )
    if not ret:
        return None
    return MemoryRegion(
        base=int(mbi.BaseAddress or 0),
        size=int(mbi.RegionSize),
        state=int(mbi.State),
        protect=int(mbi.Protect),
        type=int(mbi.Type),
    )


def _classify_address(
    addr: int,
    region: MemoryRegion | None,
    *,
    size: int = 1,
    reusable_as: str,
    rebasable: bool,
    gctab_ok: bool | None = None,
) -> dict[str, object]:
    if not addr or region is None:
        return {
            "address": addr,
            "safe_to_read": False,
            "reusable_as": reusable_as,
            "rebasable": rebasable,
            "gctab_ok": gctab_ok,
        }

    access = region.protect & 0xFF
    readable = (
        region.state == MEM_COMMIT
        and not (region.protect & PAGE_GUARD)
        and access in _READABLE_PAGE_PROTECTIONS
    )
    writable = readable and access in _WRITABLE_PAGE_PROTECTIONS
    executable = access in _EXECUTABLE_PAGE_PROTECTIONS
    safe_to_read = readable and region.base <= addr and addr + size <= region.base + region.size
    return {
        "address": addr,
        "region_base": region.base,
        "region_size": region.size,
        "state": region.state,
        "state_label": _state_label(region.state),
        "protect": region.protect,
        "protect_label": _protect_label(region.protect),
        "type": region.type,
        "type_label": _type_label(region.type),
        "readable": readable,
        "writable": writable,
        "executable": executable,
        "safe_to_read": safe_to_read,
        "reusable_as": reusable_as,
        "rebasable": rebasable,
        "gctab_ok": gctab_ok,
    }


def _read_process_bytes(pid: int, addr: int, size: int) -> bytes | None:
    k32 = ctypes.windll.kernel32
    process = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        return None
    try:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(process, ctypes.c_void_p(addr), buffer, size, ctypes.byref(read))
        return bytes(buffer) if ok and read.value == size else None
    finally:
        k32.CloseHandle(process)


def _signature_results(
    data: bytes,
    pe: PeImage,
    live_module: tuple[int, int] | None,
    running: tuple[int, Path] | None,
) -> dict[str, object]:
    results: dict[str, object] = {}
    for signature in SIGNATURES:
        pattern = _parse_pattern(signature.pattern)
        match_rows = _pattern_match_rows(data, pattern, pe, live_module, running)
        expected_ok = len(match_rows) == 1 if signature.expected_unique else bool(match_rows)

        results[signature.name] = {
            "pattern": signature.pattern,
            "purpose": signature.purpose,
            "reusable_as": signature.reusable_as,
            "expected_unique": signature.expected_unique,
            "status": "OK" if expected_ok else "CHECK",
            "match_count": len(match_rows),
            "matches": match_rows,
        }
    return results


def _normalise_pattern(pattern: str) -> str:
    return " ".join(pattern.strip().split()).upper()


def _symbol_counter(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _symbol_policy(
    name: str,
    *,
    aob_symbols: set[str],
    alloc_symbols: set[str],
    label_symbols: set[str],
    address_counts: dict[str, int],
) -> dict[str, object]:
    if name in aob_symbols:
        source = "aobscanmodule"
        reusable_as = "AOB -> RVA -> Module.Base + RVA"
        rebasable = True
    elif name in alloc_symbols:
        source = "alloc"
        reusable_as = "Cheat Engine allocation; runtime scratch only"
        rebasable = False
    elif name in label_symbols:
        source = "label"
        reusable_as = "Auto Assembler label inside injected allocation; runtime scratch only"
        rebasable = False
    else:
        source = "unknown"
        reusable_as = "Unknown Cheat Engine symbol source; do not reuse as an offset"
        rebasable = False
    return {
        "name": name,
        "source": source,
        "address_references": address_counts.get(name, 0),
        "rebasable": rebasable,
        "reusable_as": reusable_as,
    }


def _cheat_table_report(
    ct_path: Path | None,
    data: bytes,
    pe: PeImage,
    live_module: tuple[int, int] | None,
    running: tuple[int, Path] | None,
) -> dict[str, object] | None:
    if ct_path is None or not ct_path.exists():
        return None

    text = ct_path.read_text(encoding="utf-8")
    aobs = [
        {
            "symbol": match.group(1),
            "module": match.group(2),
            "pattern": _normalise_pattern(match.group(3)),
        }
        for match in _AOBSCANMODULE_RE.finditer(text)
    ]
    alloc_symbols = set(_ALLOC_RE.findall(text))
    label_symbols = set(_LABEL_RE.findall(text))
    registered_symbols = sorted(set(_REGISTER_SYMBOL_RE.findall(text)))
    address_counts = _symbol_counter(_ADDRESS_RE.findall(text))
    aob_symbols = {str(row["symbol"]) for row in aobs}

    aob_rows: list[dict[str, object]] = []
    for row in aobs:
        pattern = _parse_pattern(str(row["pattern"]))
        matches = _pattern_match_rows(data, pattern, pe, live_module, running)
        expected_ok = str(row["module"]).lower() == "t-engine.exe" and len(matches) == 1
        aob_rows.append(
            {
                **row,
                "expected_unique": True,
                "match_count": len(matches),
                "matches": matches,
                "status": "OK" if expected_ok else "CHECK",
                "reusable_as": "AOB -> RVA -> Module.Base + RVA",
            }
        )

    return {
        "path": str(ct_path),
        "sha256": _sha256(ct_path),
        "aobscanmodule_count": len(aob_rows),
        "aobscanmodules": aob_rows,
        "registered_symbols": [
            _symbol_policy(
                name,
                aob_symbols=aob_symbols,
                alloc_symbols=alloc_symbols,
                label_symbols=label_symbols,
                address_counts=address_counts,
            )
            for name in registered_symbols
        ],
        "address_symbols": address_counts,
    }


def _build_report(
    exe_path: Path,
    data: bytes,
    pe: PeImage,
    running: tuple[int, Path] | None,
    live_module: tuple[int, int] | None,
    ct_path: Path | None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "executable": {
            "path": str(exe_path),
            "size": len(data),
            "sha256": _sha256(exe_path),
            "machine": pe.machine,
            "time_date_stamp": pe.time_date_stamp,
            "pe_image_base": pe.image_base,
            "pe_image_size": pe.size_of_image,
        },
        "live_process": {
            "pid": running[0] if running else None,
            "path": str(running[1]) if running else None,
            "module_base": live_module[0] if live_module else None,
            "module_size": live_module[1] if live_module else None,
        },
        "signatures": _signature_results(data, pe, live_module, running),
        "cheat_engine_table": _cheat_table_report(ct_path, data, pe, live_module, running),
        "luajit_layout": LUAJIT_LAYOUT,
        "lua_heap_policy": {
            "rebasable": False,
            "root": "_G",
            "formula": "_G -> game -> player/level/zone/state",
            "note": "Lua GC object addresses are current-session only; rediscover and validate table chains.",
        },
    }


def _signature_rvas(row: object) -> list[int | None]:
    if not isinstance(row, dict):
        return []
    matches = row.get("matches")
    if not isinstance(matches, list):
        return []
    values: list[int | None] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        rva = match.get("rva")
        values.append(int(rva) if isinstance(rva, int) else None)
    return values


def _aob_rows(row: object) -> list[dict[str, object]]:
    if not isinstance(row, dict):
        return []
    rows = row.get("aobscanmodules")
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _compare_with_baseline(report: dict[str, object], baseline: dict[str, object]) -> list[str]:
    diffs: list[str] = []
    current_exe = report.get("executable")
    baseline_exe = baseline.get("executable")
    if isinstance(current_exe, dict) and isinstance(baseline_exe, dict):
        for key in ("size", "sha256", "machine", "time_date_stamp", "pe_image_size"):
            if current_exe.get(key) != baseline_exe.get(key):
                diffs.append(f"executable.{key}: baseline={baseline_exe.get(key)!r} current={current_exe.get(key)!r}")
    else:
        diffs.append("baseline executable block is missing or malformed")

    current_sigs = report.get("signatures")
    baseline_sigs = baseline.get("signatures")
    if not isinstance(current_sigs, dict) or not isinstance(baseline_sigs, dict):
        return diffs + ["signature block is missing or malformed"]

    for name, current in current_sigs.items():
        baseline_row = baseline_sigs.get(name)
        if not isinstance(current, dict) or not isinstance(baseline_row, dict):
            diffs.append(f"{name}: missing in baseline or malformed")
            continue
        for key in ("pattern", "status", "match_count"):
            if current.get(key) != baseline_row.get(key):
                diffs.append(f"{name}.{key}: baseline={baseline_row.get(key)!r} current={current.get(key)!r}")
        current_rvas = _signature_rvas(current)
        baseline_rvas = _signature_rvas(baseline_row)
        if current_rvas != baseline_rvas:
            diffs.append(f"{name}.rvas: baseline={baseline_rvas!r} current={current_rvas!r}")

    for name in baseline_sigs:
        if name not in current_sigs:
            diffs.append(f"{name}: present in baseline but missing from current report")

    current_ct = report.get("cheat_engine_table")
    baseline_ct = baseline.get("cheat_engine_table")
    if isinstance(current_ct, dict) or isinstance(baseline_ct, dict):
        if not isinstance(current_ct, dict) or not isinstance(baseline_ct, dict):
            diffs.append("cheat_engine_table: missing from baseline or current report")
        else:
            for key in ("sha256", "aobscanmodule_count", "address_symbols"):
                if current_ct.get(key) != baseline_ct.get(key):
                    diffs.append(
                        f"cheat_engine_table.{key}: "
                        f"baseline={baseline_ct.get(key)!r} current={current_ct.get(key)!r}"
                    )

            current_aobs = _aob_rows(current_ct)
            baseline_aobs = _aob_rows(baseline_ct)
            if len(current_aobs) != len(baseline_aobs):
                diffs.append(
                    f"cheat_engine_table.aobscanmodules: "
                    f"baseline={len(baseline_aobs)} current={len(current_aobs)}"
                )
            for index, (current_aob, baseline_aob) in enumerate(zip(current_aobs, baseline_aobs)):
                for key in ("symbol", "module", "pattern", "status", "match_count"):
                    if current_aob.get(key) != baseline_aob.get(key):
                        diffs.append(
                            f"cheat_engine_table.aobscanmodules[{index}].{key}: "
                            f"baseline={baseline_aob.get(key)!r} current={current_aob.get(key)!r}"
                        )
                current_rvas = _signature_rvas(current_aob)
                baseline_rvas = _signature_rvas(baseline_aob)
                if current_rvas != baseline_rvas:
                    diffs.append(
                        f"cheat_engine_table.aobscanmodules[{index}].rvas: "
                        f"baseline={baseline_rvas!r} current={current_rvas!r}"
                    )

    if report.get("luajit_layout") != baseline.get("luajit_layout"):
        diffs.append("luajit_layout: baseline does not match current reader assumptions")
    return diffs


def _load_baseline(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stable_baseline_report(report: dict[str, object]) -> dict[str, object]:
    stable = copy.deepcopy(report)
    stable["live_process"] = {
        "pid": None,
        "path": None,
        "module_base": None,
        "module_size": None,
    }
    for block_name in ("signatures",):
        block = stable.get(block_name)
        if not isinstance(block, dict):
            continue
        rows = block.values()
        for row in rows:
            if not isinstance(row, dict):
                continue
            matches = row.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if isinstance(match, dict):
                        match.pop("live_addr", None)
                        match.pop("live_verified", None)

    ct = stable.get("cheat_engine_table")
    if isinstance(ct, dict):
        for row in _aob_rows(ct):
            matches = row.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if isinstance(match, dict):
                        match.pop("live_addr", None)
                        match.pop("live_verified", None)
    return stable


def _write_baseline(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_stable_baseline_report(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_live_lua_roots() -> None:
    try:
        from gui.memory_reader import MemoryReader, _is_gctab, _tab_get_table, _tab_get_string
    except Exception as exc:  # noqa: BLE001
        print(f"\nLive Lua roots: unavailable ({exc})")
        return

    reader = MemoryReader()
    if not reader.attach(verbose=False):
        print("\nLive Lua roots: t-engine.exe not attached or no loaded Lua game state.")
        return
    try:
        h = reader._handle
        game_tab = reader._ensure_game_table() or 0
        player_tab = reader._ensure_player_table() or 0
        level_tab = _tab_get_table(h, game_tab, "level") if game_tab else 0
        entities_tab = _tab_get_table(h, level_tab, "entities") if level_tab else 0
        level_id = _tab_get_string(h, level_tab, "id") if level_tab else None

        print("\nLive Lua roots (current-session only, not rebasable):")
        roots = (
            ("_G", reader._global_table, "rediscover by GCtab scan"),
            ("game", game_tab, "resolve from _G.game"),
            ("game.player", player_tab, "resolve from game.player"),
            ("game.level", level_tab or 0, "resolve from game.level"),
            ("game.level.entities", entities_tab or 0, "resolve from game.level.entities"),
        )
        for name, addr, reusable_as in roots:
            row = _classify_address(
                addr,
                _query_memory_region(h, addr) if addr else None,
                size=32,
                reusable_as=reusable_as,
                rebasable=False,
                gctab_ok=_is_gctab(h, addr) if addr else False,
            )
            status = "OK" if row["safe_to_read"] and row["gctab_ok"] else "CHECK"
            if row.get("region_base") is None:
                print(f"  {name:<19} 0x{addr:08X}  {status:<5} no readable region  {reusable_as}")
                continue
            table_status = "GCtab" if row["gctab_ok"] else "not-GCtab"
            print(
                f"  {name:<19} 0x{addr:08X}  {status:<5} "
                f"{table_status} {row['type_label']}/{row['protect_label']} "
                f"base=0x{int(row['region_base']):08X}  {reusable_as}"
            )
        print(f"  game.level.id      {level_id!r}")
    finally:
        reader.detach()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate ToME memory rebase anchors.")
    parser.add_argument("--exe", type=Path, default=None, help="Path to t-engine.exe")
    parser.add_argument("--ct", type=Path, default=DEFAULT_CT_PATH, help="Path to Tales of Maj'Eyal .CT table")
    parser.add_argument("--no-ct", action="store_true", help="Do not parse the Cheat Engine table")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help="Baseline JSON used for update comparison.",
    )
    parser.add_argument("--write-baseline", action="store_true", help="Write the current report as the baseline.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero when baseline comparison differs.")
    parser.add_argument("--skip-live-lua", action="store_true", help="Do not attach with MemoryReader for Lua roots")
    args = parser.parse_args()

    running = _find_running_t_engine()
    exe_path = args.exe or (running[1] if running else None) or _default_exe()
    if exe_path is None:
        print("Could not locate t-engine.exe. Pass --exe C:\\path\\to\\t-engine.exe.", file=sys.stderr)
        return 2
    exe_path = exe_path.resolve()
    if not exe_path.exists():
        print(f"Executable not found: {exe_path}", file=sys.stderr)
        return 2

    data = exe_path.read_bytes()
    pe = _parse_pe(data)
    live_module = _module_base(running[0], running[1]) if running and running[1].resolve() == exe_path else None
    ct_path = None if args.no_ct else args.ct.resolve()
    report = _build_report(exe_path, data, pe, running, live_module, ct_path)
    executable = report["executable"]
    live_process = report["live_process"]

    print("Executable:")
    print(f"  path          {exe_path}")
    if isinstance(executable, dict):
        print(f"  size          0x{int(executable['size']):X} ({executable['size']} bytes)")
        print(f"  sha256        {executable['sha256']}")
        print(f"  machine       0x{int(executable['machine']):04X}")
        print(f"  timestamp     0x{int(executable['time_date_stamp']):08X}")
        print(f"  pe_image_base 0x{int(executable['pe_image_base']):08X}")
        print(f"  pe_image_size 0x{int(executable['pe_image_size']):X}")
    if isinstance(live_process, dict) and live_process.get("pid"):
        print(f"  live_pid      {live_process['pid']}")
        if live_process.get("module_base") is not None:
            print(f"  live_base     0x{int(live_process['module_base']):08X}")
            print(f"  live_size     0x{int(live_process['module_size']):X}")
        else:
            print("  live_base     unavailable")
    else:
        print("  live_pid      not running")

    print("\nRebasable signatures:")
    signatures = report["signatures"]
    if isinstance(signatures, dict):
        for name, row in signatures.items():
            if not isinstance(row, dict):
                continue
            print(f"  {name}: {row['status']}, matches={row['match_count']}")
            print(f"    pattern     {row['pattern']}")
            print(f"    reusable    {row['reusable_as']}")
            print(f"    purpose     {row['purpose']}")
            matches = row.get("matches")
            if isinstance(matches, list):
                for match in matches[:10]:
                    if not isinstance(match, dict):
                        continue
                    file_offset = int(match["file_offset"])
                    if "rva" not in match:
                        print(f"    file+0x{file_offset:X} -> RVA unavailable")
                        continue
                    line = f"    file+0x{file_offset:X} -> {match['section']}:RVA 0x{int(match['rva']):X}"
                    if "live_addr" in match:
                        status = "verified" if match.get("live_verified") else "not verified"
                        line += f" -> live 0x{int(match['live_addr']):08X} ({status})"
                    print(line)
                if len(matches) > 10:
                    print(f"    ... {len(matches) - 10} more matches omitted")

    ct_report = report.get("cheat_engine_table")
    if isinstance(ct_report, dict):
        print("\nCheat Engine table:")
        print(f"  path          {ct_report['path']}")
        print(f"  sha256        {ct_report['sha256']}")
        print(f"  aobscanmodule {ct_report['aobscanmodule_count']}")
        for row in _aob_rows(ct_report):
            print(f"  {row['symbol']}: {row['status']}, matches={row['match_count']}")
            print(f"    module      {row['module']}")
            print(f"    pattern     {row['pattern']}")
            print(f"    reusable    {row['reusable_as']}")
            matches = row.get("matches")
            if isinstance(matches, list):
                for match in matches[:10]:
                    if not isinstance(match, dict):
                        continue
                    file_offset = int(match["file_offset"])
                    if "rva" not in match:
                        print(f"    file+0x{file_offset:X} -> RVA unavailable")
                        continue
                    line = f"    file+0x{file_offset:X} -> {match['section']}:RVA 0x{int(match['rva']):X}"
                    if "live_addr" in match:
                        status = "verified" if match.get("live_verified") else "not verified"
                        line += f" -> live 0x{int(match['live_addr']):08X} ({status})"
                    print(line)
        symbols = ct_report.get("registered_symbols")
        if isinstance(symbols, list):
            print("  registered symbols:")
            for symbol in symbols:
                if not isinstance(symbol, dict):
                    continue
                rebase_label = "rebasable" if symbol.get("rebasable") else "runtime-only"
                print(f"    {symbol['name']:<8} {rebase_label:<12} {symbol['reusable_as']}")
    elif not args.no_ct:
        print("\nCheat Engine table: not found")

    baseline_path = args.baseline.resolve()
    if args.write_baseline:
        _write_baseline(baseline_path, report)
        print(f"\nBaseline written: {baseline_path}")

    diffs: list[str] = []
    baseline = None if args.write_baseline else _load_baseline(baseline_path)
    if args.write_baseline:
        print("Baseline comparison: skipped after write")
    elif baseline is None:
        if baseline_path.exists():
            print(f"\nBaseline comparison: could not read {baseline_path}")
        else:
            print(f"\nBaseline comparison: no baseline at {baseline_path}")
    else:
        diffs = _compare_with_baseline(report, baseline)
        if diffs:
            print(f"\nBaseline comparison: CHANGED ({len(diffs)} difference(s))")
            for diff in diffs:
                print(f"  - {diff}")
        else:
            print("\nBaseline comparison: OK")

    if not args.skip_live_lua:
        _print_live_lua_roots()

    if args.strict:
        signature_rows = report.get("signatures")
        signature_failed = (
            any(isinstance(row, dict) and row.get("status") != "OK" for row in signature_rows.values())
            if isinstance(signature_rows, dict)
            else True
        )
        if signature_failed or diffs:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
