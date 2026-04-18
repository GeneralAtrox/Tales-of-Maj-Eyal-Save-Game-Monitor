from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication

if TYPE_CHECKING:
    from gui.main_window import MainWindow


ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
CHARACTER_SHEET_SHOT = SCREENSHOT_DIR / "character-sheet-overview.png"
INVENTORY_SHOT = SCREENSHOT_DIR / "inventory-view.png"


def capture_preview_screenshots(window: MainWindow) -> None:
    """Capture the README preview screenshots from the live GUI window."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    dashboard = window._dashboard
    sheet_visual = dashboard._sheet_visual
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication is not running")

    prev_subtab = dashboard._subtabs.currentIndex()
    prev_content_tab = sheet_visual._content_tabs.currentIndex()
    try:
        dashboard._subtabs.setCurrentIndex(0)
        sheet_visual._content_tabs.setCurrentIndex(0)
        app.processEvents()
        _save_window_capture(window, CHARACTER_SHEET_SHOT)

        sheet_visual._content_tabs.setCurrentIndex(1)
        app.processEvents()
        _save_window_capture(window, INVENTORY_SHOT)
    finally:
        dashboard._subtabs.setCurrentIndex(prev_subtab)
        sheet_visual._content_tabs.setCurrentIndex(prev_content_tab)
        app.processEvents()


def capture_current_preview(window: MainWindow, preview_name: str) -> Path:
    """Capture one preview screenshot for the currently visible UI state."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    path_by_name = {
        "character-sheet-overview": CHARACTER_SHEET_SHOT,
        "inventory-view": INVENTORY_SHOT,
    }
    path = path_by_name.get(preview_name)
    if path is None:
        raise RuntimeError(f"unknown preview target: {preview_name}")
    _save_window_capture(window, path)
    return path


def _save_window_capture(window: MainWindow, path: Path) -> None:
    pixmap = window.grab()
    if pixmap.isNull() or not pixmap.save(str(path)):
        raise RuntimeError(f"failed to save screenshot to {path}")
