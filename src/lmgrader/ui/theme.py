"""Application look & feel: palette, stylesheet and small widget helpers."""
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                             QWidget, QStyleFactory)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor, QPalette

# Color sets. The module-level names below (ACCENT, BG, ...) always hold the active mode's
# colors; views read them when they build or refresh widgets.
LIGHT = dict(
    ACCENT="#4f46e5", ACCENT_HOVER="#4338ca", ACCENT_SOFT="#eef2ff", ON_ACCENT="#ffffff",
    BG="#f5f6fa", SURFACE="#ffffff", SURFACE_ALT="#f7f8fb", ROW_ALT="#fafbfd",
    HOVER="#f0f2f8", PRESSED="#e6e9f2", BORDER="#e3e6ee", BORDER_STRONG="#cfd4e2", GRID="#eef0f5",
    TEXT="#1f2433", MUTED="#6b7286", DISABLED="#a9afc2", PRIMARY_DISABLED="#a5a8e6",
    SIDEBAR="#1e2235", SIDEBAR_HOVER="#2a2f47", SIDEBAR_TEXT="#c5cae0", SIDEBAR_SUB="#8b92b0",
    CANVAS_BG="#e9ebf2", TOOLTIP_BG="#1e2235", TOOLTIP_TEXT="#ffffff",
    OK="#16a34a", WARN="#d97706", DANGER="#dc2626",
    WARN_SOFT="#fff7ed", WARN_BORDER="#fed7aa", DANGER_SOFT="#fef2f2", DANGER_BORDER="#fecaca",
)
DARK = dict(
    ACCENT="#818cf8", ACCENT_HOVER="#a5b4fc", ACCENT_SOFT="#2b2d55", ON_ACCENT="#11131f",
    BG="#14161f", SURFACE="#1d2030", SURFACE_ALT="#232738", ROW_ALT="#212435",
    HOVER="#272b3e", PRESSED="#2f3449", BORDER="#2f3448", BORDER_STRONG="#454b66", GRID="#2a2e40",
    TEXT="#e6e8f0", MUTED="#9aa1b8", DISABLED="#5c6280", PRIMARY_DISABLED="#3d4170",
    SIDEBAR="#0f111a", SIDEBAR_HOVER="#1f2333", SIDEBAR_TEXT="#b7bdd4", SIDEBAR_SUB="#7c839f",
    CANVAS_BG="#0f111a", TOOLTIP_BG="#e6e8f0", TOOLTIP_TEXT="#11131f",
    OK="#4ade80", WARN="#fbbf24", DANGER="#f87171",
    WARN_SOFT="#3a2a12", WARN_BORDER="#6b4a1a", DANGER_SOFT="#3a1a1f", DANGER_BORDER="#6b2a33",
)
MODES = ("light", "dark", "system")
mode = "light"          # active mode after apply_theme: "light" or "dark"
globals().update(LIGHT)

# overlay colors on page canvases
COLOR_ANSWER = QColor(22, 163, 74)
COLOR_QUESTION = QColor(217, 119, 6)
COLOR_NAME = QColor(147, 51, 234)
COLOR_SELECTED = QColor(79, 70, 229)

ASSETS = (Path(__file__).parent / "assets").as_posix()

_QSS = """
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QWidget#page {{ background: {BG}; }}
QWidget#sidebar {{ background: {SIDEBAR}; }}
QLabel#brand {{ color: white; font-size: 15pt; font-weight: 700; padding: 18px 18px 2px 18px; }}
QLabel#brandSub {{ color: {SIDEBAR_SUB}; font-size: 9pt; padding: 0 18px 14px 18px; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; padding: 4px 8px; }}
QListWidget#nav::item {{ color: {SIDEBAR_TEXT}; padding: 10px 12px; border-radius: 8px; margin: 2px 0; }}
QListWidget#nav::item:hover {{ background: {SIDEBAR_HOVER}; color: white; }}
QListWidget#nav::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QPushButton#sidebarBtn {{ background: transparent; border: none; color: {SIDEBAR_TEXT}; text-align: left;
    padding: 8px 20px; }}
QPushButton#sidebarBtn:hover {{ background: {SIDEBAR_HOVER}; color: white; }}
QLabel#pageTitle {{ font-size: 17pt; font-weight: 700; }}
QLabel#pageSubtitle {{ color: {MUTED}; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#pill {{ border-radius: 9px; padding: 2px 9px; font-size: 9pt; font-weight: 600; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#thumb {{ background: {SURFACE_ALT}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#banner {{ background: {WARN_SOFT}; border: 1px solid {WARN_BORDER}; border-radius: 8px; }}
QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 7px; padding: 6px 14px; }}
QPushButton:hover {{ background: {HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {PRESSED}; }}
QPushButton:disabled {{ color: {DISABLED}; background: {SURFACE_ALT}; }}
QPushButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {ACCENT}; font-weight: 600; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: {ON_ACCENT}; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:disabled {{ background: {PRIMARY_DISABLED}; border-color: {PRIMARY_DISABLED}; }}
QPushButton[kind="danger"] {{ color: {DANGER}; }}
QPushButton[kind="danger"]:hover {{ background: {DANGER_SOFT}; border-color: {DANGER_BORDER}; }}
QPushButton[kind="ghost"] {{ border: none; background: transparent; color: {ACCENT}; }}
QPushButton[kind="ghost"]:hover {{ background: {ACCENT_SOFT}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 7px; padding: 5px 8px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow, QAbstractSpinBox::down-arrow {{ image: url("{ASSETS}/chevron-down.svg"); width: 10px; height: 10px; }}
QAbstractSpinBox::up-arrow {{ image: url("{ASSETS}/chevron-up.svg"); width: 10px; height: 10px; }}
QAbstractSpinBox {{ padding-right: 20px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 18px;
    border: none; background: transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {ACCENT_SOFT}; }}
QComboBox QAbstractItemView {{ border: 1px solid {BORDER}; selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT}; background: {SURFACE}; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: {GRID}; alternate-background-color: {ROW_ALT}; outline: none; }}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{ padding: 5px; }}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {ACCENT_SOFT}; color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE_ALT}; border: none; border-bottom: 1px solid {BORDER};
    padding: 7px 6px; font-weight: 600; color: {MUTED}; }}
QTableCornerButton::section {{ background: {SURFACE_ALT}; border: none; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea#canvasScroll {{ background: {CANVAS_BG}; border: 1px solid {BORDER}; border-radius: 8px; }}
QProgressBar {{ border: none; background: {PRESSED}; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QSplitter::handle {{ background: transparent; }}
QCheckBox {{ spacing: 6px; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QToolTip {{ background: {TOOLTIP_BG}; color: {TOOLTIP_TEXT}; border: none; padding: 5px; }}
QDialog, QMessageBox, QInputDialog, QFileDialog, QProgressDialog {{ background: {SURFACE}; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
"""

def stylesheet(colors: dict) -> str:
    return _QSS.format(ASSETS=ASSETS, **colors)

STYLESHEET = stylesheet(LIGHT)

def palette(colors: dict) -> QPalette:
    """Fixed palette for the chosen mode: Fusion would otherwise follow the Windows color scheme,
    which once turned dialogs black while the stylesheet forced dark text."""
    c = colors
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: c["BG"], QPalette.ColorRole.WindowText: c["TEXT"],
        QPalette.ColorRole.Base: c["SURFACE"], QPalette.ColorRole.AlternateBase: c["ROW_ALT"],
        QPalette.ColorRole.Text: c["TEXT"], QPalette.ColorRole.Button: c["SURFACE"],
        QPalette.ColorRole.ButtonText: c["TEXT"], QPalette.ColorRole.ToolTipBase: c["TOOLTIP_BG"],
        QPalette.ColorRole.ToolTipText: c["TOOLTIP_TEXT"], QPalette.ColorRole.Highlight: c["ACCENT"],
        QPalette.ColorRole.HighlightedText: c["ON_ACCENT"], QPalette.ColorRole.PlaceholderText: c["MUTED"],
        QPalette.ColorRole.Link: c["ACCENT"], QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Light: c["BORDER_STRONG"], QPalette.ColorRole.Midlight: c["BORDER"],
        QPalette.ColorRole.Mid: c["BORDER"], QPalette.ColorRole.Dark: c["BORDER_STRONG"],
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["DISABLED"]))
    return p

def light_palette() -> QPalette:
    return palette(LIGHT)

def _system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return False

def resolve_mode(choice: str, app: QApplication = None) -> str:
    """'light' | 'dark' | 'system' -> 'light' or 'dark'."""
    if choice == "system":
        return "dark" if _system_is_dark(app or QApplication.instance()) else "light"
    return "dark" if choice == "dark" else "light"

def apply_theme(app: QApplication, choice: str = None):
    """Apply light or dark mode (choice: 'light', 'dark', 'system'; default: the saved setting).
    Can be called again at runtime; open views pick up inline colors on their next refresh."""
    global mode
    if choice is None:
        from ..config import UI_CONFIG
        choice = UI_CONFIG.get("theme", "light")
    app.setStyle(QStyleFactory.create("Fusion"))
    if choice == "system":
        try:  # drop an earlier pin so the OS scheme can be read
            app.styleHints().unsetColorScheme()
        except AttributeError:
            pass
    mode = resolve_mode(choice, app)
    colors = DARK if mode == "dark" else LIGHT
    globals().update(colors)
    try:  # Qt >= 6.8: pin the scheme so native parts (dialogs, title bar) match
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if mode == "dark" else Qt.ColorScheme.Light)
    except AttributeError:
        pass
    app.setPalette(palette(colors))
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(stylesheet(colors))

def button(text: str, kind: str = "", tooltip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if kind == "primary":   # the QSS makes it bold; a bold widget font makes the size hint fit the text
        f = b.font(); f.setWeight(QFont.Weight.DemiBold); b.setFont(f)
    if tooltip:
        b.setToolTip(tooltip)
    return b

def card(layout_cls=QVBoxLayout, margins=14, spacing=10):
    """Returns (frame, layout)."""
    f = QFrame()
    f.setObjectName("card")
    lay = layout_cls(f)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    return f, lay

def label(text: str, obj: str = "") -> QLabel:
    l = QLabel(text)
    if obj:
        l.setObjectName(obj)
    return l

def pill(text: str, color: str) -> QLabel:
    l = QLabel(text)
    l.setObjectName("pill")
    c = QColor(color)
    l.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},30); color: {color};")
    return l

def setup_page(w: QWidget, title: str, subtitle: str = ""):
    """Give a view a page header. Returns (body layout, header actions layout)."""
    w.setObjectName("page")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    outer = QVBoxLayout(w)
    outer.setContentsMargins(24, 20, 24, 20)
    outer.setSpacing(14)
    head = QHBoxLayout()
    titles = QVBoxLayout()
    titles.setSpacing(2)
    titles.addWidget(label(title, "pageTitle"))
    if subtitle:
        titles.addWidget(label(subtitle, "pageSubtitle"))
    head.addLayout(titles)
    head.addStretch()
    actions = QHBoxLayout()
    actions.setSpacing(8)
    head.addLayout(actions)
    outer.addLayout(head)
    body = QVBoxLayout()
    body.setSpacing(12)
    outer.addLayout(body, 1)
    return body, actions
