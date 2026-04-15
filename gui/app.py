from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from gui.theme import STYLESHEET


def _is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _relaunch_elevated() -> bool:
    """Re-run this script via UAC elevation. Returns True if launched."""
    shell32 = ctypes.windll.shell32
    script  = sys.argv[0]
    params  = " ".join(f'"{a}"' for a in sys.argv[1:])
    # SW_SHOWNORMAL = 1
    ret = shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}', None, 1)
    return ret > 32


def main() -> None:
    # ── Request Administrator if not already elevated ──
    if not _is_admin():
        if _relaunch_elevated():
            sys.exit(0)  # elevated child is starting — exit this one
        # UAC was declined or failed — continue without memory reading
        print("[!] Running without Administrator — live HP reading disabled.")

    # High-DPI scaling (Qt 6 default) — kept explicit for clarity
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName("TOME Save Monitor")
    app.setStyleSheet(STYLESHEET)

    config_path = Path("config.json")
    window = MainWindow(config_path)
    window.show()

    sys.exit(app.exec())
