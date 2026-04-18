"""
find_lua_state.py
-----------------
Reads game.player.life / max_life from t-engine.exe (LuaJIT 2.0.2).

Scans memory for large GCtab objects (gct=0x0B), finds the one
containing "game", then walks game -> player -> life/max_life.

Run from an Administrator terminal:
    python tools/find_lua_state.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import sys
import time

# ── Win32 ─────────────────────────────────────────────────────────────────────
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

k32 = ctypes.windll.kernel32


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.wintypes.DWORD),
        ("Protect", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
    ]


# ── LuaJIT 2.0.2 constants (32-bit, no GC64) ────────────────────────────────
#
# GC header gct values (plain type index, NOT the ~itype used in TValues):
#   GCstr  = 4   (0x04)
#   GCtab  = 11  (0x0B)
#   Thread = 6   (0x06)
#
# TValue itype values (upper 32 bits of NaN-boxed 64-bit value):
#   LJ_TSTR  = ~4u  = 0xFFFFFFFB
#   LJ_TTAB  = ~11u = 0xFFFFFFF4
#   LJ_TNIL  = ~0u  = 0xFFFFFFFF
#   number: itype < ~13u (0xFFFFFFF2)

GCT_STR = 0x04  # gct byte in GCstr header
GCT_TAB = 0x0B  # gct byte in GCtab header
GCT_THREAD = 0x06  # gct byte in lua_State header

LJ_TSTR = 0xFFFFFFFB  # TValue itype for string
LJ_TTAB = 0xFFFFFFF4  # TValue itype for table
LJ_TNIL = 0xFFFFFFFF
LJ_TNUMX = 0xFFFFFFF2  # itype < this means it's a double

NODE_SIZE = 24  # sizeof(Node) in 32-bit LuaJIT 2

# ── Memory helpers ────────────────────────────────────────────────────────────


def _open_process(pid: int) -> int:
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        err = k32.GetLastError()
        if err == 5:
            raise OSError("Access Denied - run as Administrator")
        raise OSError(f"OpenProcess error {err}")
    return h


def _read(h: int, addr: int, n: int) -> bytes | None:
    buf = ctypes.create_string_buffer(n)
    read = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(read))
    return bytes(buf) if (ok and read.value == n) else None


def ru32(h: int, addr: int) -> int | None:
    b = _read(h, addr, 4)
    return struct.unpack("<I", b)[0] if b else None


def rf64(h: int, addr: int) -> float | None:
    b = _read(h, addr, 8)
    return struct.unpack("<d", b)[0] if b else None


def is_heap(v: int) -> bool:
    return 0x00400000 <= v < 0xFFFF0000


# ── Region iterator ────────────────────────────────────────────────────────────


def iter_regions(h: int):
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()
    while True:
        ret = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        ok = (
            mbi.State == MEM_COMMIT
            and not (mbi.Protect & PAGE_NOACCESS)
            and not (mbi.Protect & PAGE_GUARD)
            and size > 0
        )
        if ok:
            data = _read(h, base, size)
            if data:
                yield base, data
        addr = base + size
        if addr >= 0xFFFFFFFF:
            break


# ── GCtab traversal ───────────────────────────────────────────────────────────


def tab_find_strkey(h: int, tab_ptr: int, key: str) -> int | None:
    """Return address of val TValue for string key, or None."""
    key_b = key.encode()
    node_ptr = ru32(h, tab_ptr + 20)
    hmask = ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not is_heap(node_ptr):
        return None
    total_bytes = (hmask + 1) * NODE_SIZE
    if total_bytes > 16 * 1024 * 1024:
        return None
    bulk = _read(h, node_ptr, total_bytes)
    if not bulk:
        return None
    for i in range(hmask + 1):
        off = i * NODE_SIZE
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != LJ_TSTR:
            continue
        gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        if not is_heap(gcs):
            continue
        slen_raw = _read(h, gcs + 12, 4)
        if not slen_raw:
            continue
        slen = struct.unpack("<I", slen_raw)[0]
        if slen != len(key_b):
            continue
        raw = _read(h, gcs + 16, slen)
        if raw == key_b:
            return node_ptr + off
    return None


def tab_get_table(h: int, tab_ptr: int, key: str) -> int | None:
    node = tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    if ru32(h, node + 4) != LJ_TTAB:
        return None
    v = ru32(h, node)
    return v if (v and is_heap(v)) else None


def tab_get_number(h: int, tab_ptr: int, key: str) -> float | None:
    node = tab_find_strkey(h, tab_ptr, key)
    if node is None:
        return None
    it = ru32(h, node + 4)
    if it is None or it >= LJ_TNUMX:
        return None
    return rf64(h, node)


def tab_str_keys(h: int, tab_ptr: int, limit: int = 20) -> list[str]:
    node_ptr = ru32(h, tab_ptr + 20)
    hmask = ru32(h, tab_ptr + 28)
    if not node_ptr or hmask is None or not is_heap(node_ptr):
        return []
    total = min((hmask + 1) * NODE_SIZE, 4096 * NODE_SIZE)
    bulk = _read(h, node_ptr, total)
    if not bulk:
        return []
    keys: list[str] = []
    for i in range(min(hmask + 1, 4096)):
        off = i * NODE_SIZE
        key_it = struct.unpack_from("<I", bulk, off + 12)[0]
        if key_it != LJ_TSTR:
            continue
        gcs = struct.unpack_from("<I", bulk, off + 8)[0]
        if not is_heap(gcs):
            continue
        meta = _read(h, gcs + 12, 4)
        if not meta:
            continue
        slen = struct.unpack("<I", meta)[0]
        if not (0 < slen < 64):
            continue
        raw = _read(h, gcs + 16, slen)
        if raw:
            try:
                keys.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        if len(keys) >= limit:
            break
    return keys


# ── GCtab scanner ──────────────────────────────────────────────────────────────


def scan_for_global_tables(h: int) -> list[int]:
    """
    Scan for GCtab objects (gct=0x0B at +5) with hmask >= 63,
    then check if they contain the key "game".
    """
    results: list[int] = []
    tab_candidates = 0
    large_tabs = 0

    print("Phase 1: Scanning for large GCtab objects (gct=0x0B)...")
    t0 = time.time()

    for base, data in iter_regions(h):
        dlen = len(data)
        for off in range(0, dlen - 32, 4):
            if data[off + 5] != GCT_TAB:
                continue
            tab_candidates += 1

            if off + 32 > dlen:
                continue
            node_ptr = struct.unpack_from("<I", data, off + 20)[0]
            hmask = struct.unpack_from("<I", data, off + 28)[0]

            if hmask < 63 or hmask > 0xFFFF:
                continue
            if not is_heap(node_ptr):
                continue

            large_tabs += 1
            results.append(base + off)

    elapsed = time.time() - t0
    print(f"  {tab_candidates} GCtab candidates, {large_tabs} large (hmask>=63)")
    print(f"  Scan took {elapsed:.1f}s")
    return results


# ── Process helper ────────────────────────────────────────────────────────────


def get_pid(name: str) -> int | None:
    import subprocess

    r = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    )
    for line in r.stdout.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            return int(parts[1])
    return None


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    pid = get_pid("t-engine.exe")
    if pid is None:
        print("t-engine.exe not found - is the game running?")
        sys.exit(1)
    print(f"Found t-engine.exe  PID={pid}\n")

    h = _open_process(pid)

    large_tabs = scan_for_global_tables(h)

    if not large_tabs:
        print("No large GCtab objects found.")
        k32.CloseHandle(h)
        sys.exit(1)

    print(f"\nPhase 2: Checking {len(large_tabs)} large tables for 'game' key...")
    t0 = time.time()
    found = False

    for i, tab_ptr in enumerate(large_tabs):
        if (i + 1) % 100 == 0:
            print(f"  checked {i + 1}/{len(large_tabs)}...")

        game_node = tab_find_strkey(h, tab_ptr, "game")
        if game_node is None:
            continue

        hmask = ru32(h, tab_ptr + 28)
        keys = tab_str_keys(h, tab_ptr, limit=15)
        print(f"\n  _G found: 0x{tab_ptr:08X}  hmask={hmask}")
        print(f"  sample keys: {keys}")

        game_tab = tab_get_table(h, tab_ptr, "game")
        if game_tab is None:
            print("  'game' is not a table value, skipping")
            continue
        print(f"  game table = 0x{game_tab:08X}")

        player_tab = tab_get_table(h, game_tab, "player")
        if player_tab is None:
            print("  'player' not in game  (load a character in-game first)")
            continue
        print(f"  player table = 0x{player_tab:08X}")

        life = tab_get_number(h, player_tab, "life")
        max_life = tab_get_number(h, player_tab, "max_life")

        if life is not None and max_life is not None:
            print()
            print(f"  *** HP: {life:.0f} / {max_life:.0f} ***")
            print(f"  _G       = 0x{tab_ptr:08X}")
            print(f"  player   = 0x{player_tab:08X}")
            found = True
        else:
            pkeys = tab_str_keys(h, player_tab, limit=20)
            print(f"  life/max_life not found. Player keys: {pkeys}")

    elapsed = time.time() - t0
    print(f"\nPhase 2 took {elapsed:.1f}s")
    if not found:
        print("Could not find game.player.life.")

    k32.CloseHandle(h)


if __name__ == "__main__":
    main()
