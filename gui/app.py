from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
import pyuac
import win32con
import win32gui
import win32process

from gui.main_window import MainWindow
from gui.theme import STYLESHEET


def _debug_mode_active() -> bool:
    """Return True when running under a debugger/IDE hosted session."""
    if sys.gettrace() is not None:
        return True
    return os.environ.get("PYCHARM_HOSTED") == "1"


def _script_markers() -> set[str]:
    """Return stable lowercase markers that identify this GUI launch."""
    markers = {
        str(Path.cwd()).lower(),
        Path(sys.argv[0]).name.lower(),
        "tome_savemonitor_gui.py",
        "gui\\app.py",
        "tome save monitor",
    }
    return markers


def _pid_command_line(pid: int) -> str:
    """Return the command line for a process id via PowerShell CIM."""
    import subprocess

    cmd = (
        f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def _find_existing_windows(window_title: str) -> list[tuple[int, int]]:
    """Return ``(hwnd, pid)`` pairs for matching GUI windows owned by Python processes."""
    current_pid = os.getpid()
    markers = _script_markers()
    matches: list[tuple[int, int]] = []

    def enum_proc(hwnd, lparam) -> bool:  # noqa: ARG001
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title != window_title:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid or pid == current_pid:
            return True
        cmdline = _pid_command_line(pid)
        if "python" not in cmdline:
            return True
        if not any(marker in cmdline for marker in markers):
            return True
        matches.append((hwnd, pid))
        return True

    win32gui.EnumWindows(enum_proc, 0)
    deduped: list[tuple[int, int]] = []
    seen: set[int] = set()
    for hwnd, pid in matches:
        if pid in seen:
            continue
        seen.add(pid)
        deduped.append((hwnd, pid))
    return deduped


def _request_existing_shutdown(window_title: str, timeout_s: float = 5.0) -> None:
    """Close any existing GUI windows for this app before launching a new one."""
    import subprocess

    deadline = time.monotonic() + timeout_s
    force_after = time.monotonic() + 1.0
    while True:
        windows = _find_existing_windows(window_title)
        if not windows:
            return

        for hwnd, pid in windows:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
            if time.monotonic() >= force_after:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                except Exception:
                    pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.25)


def _relaunch_elevated() -> bool:
    """Start an elevated copy of the app and return True when launch succeeds."""
    try:
        pyuac.runAsAdmin(wait=False)
    except Exception:
        return False
    return True


def main() -> None:
    # ── Request Administrator if not already elevated ──
    if not pyuac.isUserAdmin():
        if _debug_mode_active():
            print("[!] Debug session detected — UAC relaunch will replace the debug process.")
        if _relaunch_elevated():
            sys.exit(0)
        print("[!] Running without Administrator — live HP reading disabled.")

    # ── Kick off the t-engine.exe attach in a background thread NOW, so it
    #    runs in parallel with QApplication setup, existing-instance shutdown,
    #    and MainWindow construction.  By the time the dashboard needs the
    #    reader, the Lua _G scan is typically already complete.
    from gui.memory_reader import start_background_preattach
    start_background_preattach()

    # High-DPI scaling (Qt 6 default) — kept explicit for clarity
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName("ToME - Scrying Mirror")
    app.setWindowIcon(QIcon(str(Path(__file__).parent.parent / "Icons" / "app" / "scrying_mirror.png")))
    app.setStyleSheet(STYLESHEET)
    _request_existing_shutdown("ToME - Scrying Mirror")

    config_path = Path("config.json")
    window = MainWindow(config_path)
    window.show()

    sys.exit(app.exec())
