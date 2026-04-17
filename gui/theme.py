from __future__ import annotations

# Catppuccin Mocha-inspired dark palette
BG       = "#1e1e2e"
SURFACE0 = "#181825"
SURFACE1 = "#313244"
SURFACE2 = "#45475a"
OVERLAY  = "#6c7086"
TEXT     = "#cdd6f4"
SUBTEXT1 = "#bac2de"
SUBTEXT0 = "#a6adc8"
BLUE     = "#89b4fa"
GREEN    = "#a6e3a1"
YELLOW   = "#f9e2af"
RED      = "#f38ba8"
MAUVE    = "#cba6f7"
TEAL     = "#94e2d5"
BORDER   = "#313244"

STYLESHEET = f"""
/* ── Global ── */
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {TEXT};
    selection-background-color: {BLUE};
    selection-color: {SURFACE0};
}}
QMainWindow, QDialog, QWidget {{
    background: {BG};
}}

/* ── Tabs ── */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {BG};
    border-radius: 4px;
}}
QTabBar::tab {{
    background: {SURFACE0};
    color: {SUBTEXT0};
    padding: 9px 22px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {SURFACE2};
    color: {TEXT};
    border-bottom: 3px solid {BLUE};
}}
QTabBar::tab:hover:!selected {{
    background: {SURFACE1};
    color: {SUBTEXT1};
}}

/* ── Buttons ── */
QPushButton, QToolButton {{
    background: {SURFACE1};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 26px;
}}
QPushButton:hover, QToolButton:hover {{
    background: {SURFACE2};
    border-color: {BLUE};
}}
QPushButton:pressed, QToolButton:pressed {{
    background: {BG};
}}
QPushButton:disabled, QToolButton:disabled {{
    color: {OVERLAY};
    border-color: {SURFACE1};
}}
QPushButton[accent="true"] {{
    background: {BLUE};
    color: {SURFACE0};
    border: none;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {TEAL};
}}

/* ── Dropdown (QToolButton with menu) ── */
QToolButton[popupMode="2"] {{
    padding-right: 6px;
}}

/* ── Menus ── */
QMenu {{
    background: {SURFACE1};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px 6px 12px;
    border-radius: 3px;
    color: {TEXT};
}}
QMenu::item:selected {{
    background: {SURFACE2};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 0;
}}

/* ── Inputs ── */
QLineEdit, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {SURFACE0};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {BLUE};
}}
QPlainTextEdit[readOnly="true"] {{
    border-color: {BORDER};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: {SURFACE1};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {SURFACE2};
}}

/* ── ComboBox ── */
QComboBox {{
    background: {SURFACE0};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT};
    min-height: 26px;
}}
QComboBox:focus {{
    border-color: {BLUE};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE1};
    border: 1px solid {BORDER};
    selection-background-color: {SURFACE2};
    color: {TEXT};
    outline: none;
}}

/* ── Tables ── */
QTableWidget, QTableView {{
    background: {SURFACE0};
    alternate-background-color: {BG};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 4px;
    selection-background-color: {SURFACE1};
    selection-color: {TEXT};
    outline: none;
}}
QHeaderView::section {{
    background: {SURFACE1};
    color: {SUBTEXT0};
    padding: 6px 10px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.5px;
}}
QHeaderView::section:last {{
    border-right: none;
}}
QTableWidget::item {{
    padding: 4px 8px;
    border: none;
}}
QTableWidget::item:selected {{
    background: {SURFACE1};
    color: {TEXT};
}}

/* ── Scrollbars ── */
QScrollBar:vertical {{
    background: {SURFACE0};
    width: 8px;
    border-radius: 4px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE2};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {OVERLAY};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {SURFACE0};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {SURFACE2};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {OVERLAY};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── Splitter ── */
QSplitter::handle {{
    background: {BORDER};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}

/* ── Labels ── */
QLabel {{
    background: transparent;
}}
QLabel[heading="true"] {{
    font-size: 15px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel[subheading="true"] {{
    font-size: 12px;
    color: {SUBTEXT0};
}}
QLabel[status="ok"]    {{ color: {GREEN};  }}
QLabel[status="warn"]  {{ color: {YELLOW}; }}
QLabel[status="error"] {{ color: {RED};    }}

/* ── Status bar ── */
QStatusBar {{
    background: {SURFACE0};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT0};
    font-size: 12px;
}}

/* ── GroupBox ── */
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 12px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    color: {SUBTEXT0};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}

/* ── List widget ── */
QListWidget {{
    background: {SURFACE0};
    border: 1px solid {BORDER};
    border-radius: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 5px 8px;
    border-radius: 3px;
    color: {TEXT};
}}
QListWidget::item:selected {{
    background: {SURFACE1};
}}
QListWidget::item:hover:!selected {{
    background: {SURFACE1};
}}

/* ── Tooltip ── */
QToolTip {{
    background: {SURFACE1};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 4px 8px;
}}
"""
