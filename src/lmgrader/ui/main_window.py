"""Main window: sidebar navigation + stacked pages."""
from PyQt6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
                             QStackedWidget, QPushButton, QApplication)
from PyQt6.QtCore import Qt, QSize

from ..config import APP_VERSION, UI_CONFIG, save_settings
from ..grading import session
from . import theme
from .theme import label
from .students_view import StudentsView
from .template_editor import TemplateEditorView
from .scan_view import ScanView
from .review_view import ReviewView
from .return_view import ReturnView
from .settings_view import SettingsView

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LMGrader")
        self.resize(1400, 880)
        self.setMinimumSize(1100, 700)
        # gradings cut off by a crash / power loss go back to "ready"; saved questions are kept
        self.recovered = session.recover_interrupted()

        self.students_view = StudentsView()
        self.template_view = TemplateEditorView()
        self.scan_view = ScanView(self.template_view, self.students_view)
        self.review_view = ReviewView()
        self.return_view = ReturnView()
        self.settings_view = SettingsView()

        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(210)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 12)
        sl.addWidget(label("LMGrader", "brand"))
        sl.addWidget(label("Local exam grading", "brandSub"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(0, 0))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sl.addWidget(self.nav, 1)
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("sidebarBtn")
        self.btn_theme.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_theme.clicked.connect(lambda: self.set_theme("light" if theme.mode == "dark" else "dark"))
        sl.addWidget(self.btn_theme)
        self._theme_label()
        ver = label(f"v{APP_VERSION}", "brandSub")
        sl.addWidget(ver)
        lay.addWidget(sidebar)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        for text, view in [("Exams & Templates", self.template_view), ("Students", self.students_view),
                           ("Scan & Grade", self.scan_view), ("Review", self.review_view),
                           ("Return", self.return_view), ("Settings", self.settings_view)]:
            self.nav.addItem(QListWidgetItem(text))
            self.stack.addWidget(view)
        self.nav.currentRowChanged.connect(self._on_nav)
        self.nav.setCurrentRow(0)

        self.setCentralWidget(root)
        if self.recovered:
            self.statusBar().showMessage(f"Recovered {self.recovered} interrupted grading"
                                         f"{'s' if self.recovered != 1 else ''} — they will resume "
                                         "from the last saved question (Scan & Grade).")
        else:
            self.statusBar().showMessage("Ready")

    def _theme_label(self):
        self.btn_theme.setText("☀  Light mode" if theme.mode == "dark" else "☾  Dark mode")
        self.btn_theme.setToolTip("Switch the colors of LMGrader (also in Settings → Appearance)")

    def set_theme(self, choice: str):
        """Switch light/dark/system now and remember it."""
        UI_CONFIG["theme"] = choice
        save_settings()
        theme.apply_theme(QApplication.instance(), choice)
        self._theme_label()
        self.settings_view.sync_theme()
        # views with inline colors (status text, warnings, step labels) rebuild them
        self.template_view._render_steps()
        self._on_nav(self.nav.currentRow())

    def _on_nav(self, row):
        self.stack.setCurrentIndex(row)
        w = self.stack.widget(row)
        if w is self.review_view:
            self.review_view.refresh()
        elif w is self.scan_view:
            self.scan_view.refresh()
        elif w is self.return_view:
            self.return_view.refresh()

    def closeEvent(self, e):
        tv = self.template_view
        if tv.dirty and not tv._confirm_discard():
            e.ignore()
            return
        self.scan_view.shutdown()   # unfinished work is persisted and resumes next start
        self.return_view.shutdown()  # sent emails are recorded; re-running skips them
        super().closeEvent(e)
