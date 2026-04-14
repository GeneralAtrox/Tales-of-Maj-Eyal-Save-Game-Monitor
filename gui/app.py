from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from gui.theme import STYLESHEET


def main() -> None:
    # High-DPI scaling (Qt 6 default) — kept explicit for clarity
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    app = QApplication(sys.argv)
    app.setApplicationName("TOME Save Monitor")
    app.setStyleSheet(STYLESHEET)

    config_path = Path("config.json")
    window = MainWindow(config_path)
    window.show()

    sys.exit(app.exec())
