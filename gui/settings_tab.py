from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from models import AppConfig


class SettingsTab(QWidget):
    """Config editor.

    Groups related fields into QGroupBoxes.  Grouped actions (Save / Reload /
    Reset) are surfaced as a single dropdown QToolButton so the toolbar stays
    compact.
    """

    config_saved = Signal(object)    # emits AppConfig
    config_reloaded = Signal(object) # emits AppConfig (for status bar)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: AppConfig | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 20)
        root.setSpacing(16)

        title = QLabel("Settings")
        title.setProperty("heading", True)
        root.addWidget(title)

        # ── Paths ──────────────────────────────────────────────────────────
        paths_box = QGroupBox("Paths")
        paths_form = QFormLayout(paths_box)
        paths_form.setSpacing(10)
        paths_form.setContentsMargins(12, 16, 12, 12)

        self._save_root_edit = QLineEdit()
        self._save_root_edit.setPlaceholderText("~/T-Engine/4.0/tome/save")

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_save_root)

        save_root_row = QHBoxLayout()
        save_root_row.setSpacing(8)
        save_root_row.addWidget(self._save_root_edit)
        save_root_row.addWidget(browse_btn)
        paths_form.addRow("Save Root:", save_root_row)
        root.addWidget(paths_box)

        # ── Te4.org Account ────────────────────────────────────────────────
        account_box = QGroupBox("Te4.org Account")
        account_form = QFormLayout(account_box)
        account_form.setSpacing(10)
        account_form.setContentsMargins(12, 16, 12, 12)

        self._profile_id_edit = QLineEdit()
        self._profile_id_edit.setPlaceholderText("Numeric profile ID (e.g. 12345)")
        account_form.addRow("Profile ID:", self._profile_id_edit)
        root.addWidget(account_box)

        # ── Backups ────────────────────────────────────────────────────────
        backup_box = QGroupBox("Backups")
        backup_form = QFormLayout(backup_box)
        backup_form.setSpacing(10)
        backup_form.setContentsMargins(12, 16, 12, 12)

        self._backup_limit_spin = QSpinBox()
        self._backup_limit_spin.setRange(1, 100)
        self._backup_limit_spin.setValue(3)
        self._backup_limit_spin.setFixedWidth(80)
        self._backup_limit_spin.setToolTip(
            "Maximum number of timestamped backups kept per character.\n"
            "Oldest are removed when the limit is exceeded."
        )
        backup_form.addRow("Keep last N backups:", self._backup_limit_spin)
        root.addWidget(backup_box)

        root.addStretch()

        # ── Action bar ─────────────────────────────────────────────────────
        action_bar = QHBoxLayout()
        action_bar.addStretch()

        # "Config" dropdown button — save / reload / reset to defaults
        config_btn = QToolButton()
        config_btn.setText("Config  ▾")
        config_btn.setFixedWidth(115)
        config_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        config_menu = QMenu(config_btn)
        config_menu.addAction("Save",            self._save_config)
        config_menu.addAction("Reload",          self._reload_fields)
        config_menu.addSeparator()
        config_menu.addAction("Reset to Defaults", self._reset_defaults)
        config_btn.setMenu(config_menu)

        action_bar.addWidget(config_btn)
        root.addLayout(action_bar)

    # ── Public API ────────────────────────────────────────────────────────

    def load_config(self, config: AppConfig) -> None:
        self._config = config
        self._populate_fields(config)

    # ── Internals ─────────────────────────────────────────────────────────

    def _populate_fields(self, config: AppConfig) -> None:
        self._save_root_edit.setText(str(config.save_root))
        self._profile_id_edit.setText(config.profile_id or "")
        self._backup_limit_spin.setValue(config.backup_limit)

    def _browse_save_root(self) -> None:
        start = self._save_root_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select Save Directory", start)
        if chosen:
            self._save_root_edit.setText(chosen)

    def _save_config(self) -> None:
        if not self._config:
            return
        self._config.save_root    = Path(self._save_root_edit.text()).expanduser()
        self._config.profile_id   = self._profile_id_edit.text().strip()
        self._config.backup_limit = self._backup_limit_spin.value()
        self.config_saved.emit(self._config)

    def _reload_fields(self) -> None:
        if self._config:
            self._populate_fields(self._config)

    def _reset_defaults(self) -> None:
        defaults = AppConfig()
        self._save_root_edit.setText(str(defaults.save_root))
        self._profile_id_edit.setText("")
        self._backup_limit_spin.setValue(defaults.backup_limit)
