from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.main_window import MainWindow  # noqa: E402
from gui.preview_capture import SCREENSHOT_DIR, capture_preview_screenshots  # noqa: E402


README_PREVIEW_RE = re.compile(
    r"Current Preview \d{2}/\d{2}/\d{4}\r?\n"
    r"(?:<img[^\n]*>\r?\n|!\[[^\n]*\]\([^)]+\)\r?\n)+",
    re.MULTILINE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture repo-hosted README preview screenshots from the GUI.",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.json"),
        help="Path to the GUI config file to load.",
    )
    parser.add_argument(
        "--readme",
        default=str(ROOT / "README.md"),
        help="README file to update.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15000,
        help="Fail if the GUI is not ready within this time.",
    )
    return parser.parse_args()
def _update_readme(readme_path: Path) -> None:
    text = readme_path.read_text(encoding="utf-8")
    date_str = datetime.now().strftime("%d/%m/%Y")
    overview_rel = SCREENSHOT_DIR.relative_to(ROOT).as_posix() + "/character-sheet-overview.png"
    inventory_rel = SCREENSHOT_DIR.relative_to(ROOT).as_posix() + "/inventory-view.png"
    replacement = (
        f"Current Preview {date_str}\n"
        f"![Character Sheet overview]({overview_rel})\n"
        f"![Inventory view]({inventory_rel})\n"
    )
    updated, count = README_PREVIEW_RE.subn(replacement, text, count=1)
    if count == 0:
        raise RuntimeError("could not find the README preview block to update")
    readme_path.write_text(updated, encoding="utf-8")


def main() -> int:
    args = _parse_args()
    config_path = Path(args.config).resolve()
    readme_path = Path(args.readme).resolve()

    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1
    if not readme_path.exists():
        print(f"README not found: {readme_path}", file=sys.stderr)
        return 1

    app = QApplication([])
    window = MainWindow(config_path)
    window.resize(1729, 1097)
    window.show()

    result = {"code": 1, "message": "timed out waiting for GUI readiness"}

    def fail(message: str) -> None:
        result["code"] = 1
        result["message"] = message
        window.close()
        app.quit()

    def succeed() -> None:
        try:
            capture_preview_screenshots(window)
            _update_readme(readme_path)
        except Exception as exc:  # noqa: BLE001
            fail(str(exc))
            return

        result["code"] = 0
        result["message"] = "preview screenshots updated"
        window.close()
        app.quit()

    def wait_for_ready() -> None:
        config_loaded = window._monitor.config is not None
        chars_loaded = bool(window._char_items)
        if config_loaded and chars_loaded:
            QTimer.singleShot(250, succeed)
            return
        QTimer.singleShot(250, wait_for_ready)

    QTimer.singleShot(args.timeout_ms, lambda: fail("timed out waiting for GUI readiness"))
    QTimer.singleShot(250, wait_for_ready)
    app.exec()

    if result["code"] != 0:
        print(result["message"], file=sys.stderr)
    else:
        print(result["message"])
    return int(result["code"])


if __name__ == "__main__":
    raise SystemExit(main())
