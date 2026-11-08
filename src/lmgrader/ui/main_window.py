"""Main window: sidebar navigation + stacked pages."""
from PyQt6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
                             QStackedWidget, QPushButton, QApplication, QComboBox, QMessageBox)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QKeySequence, QShortcut

from ..config import APP_VERSION, UI_CONFIG, save_settings, llm_enabled
from ..db import list_courses, current_course, set_current_course, course_name
from ..grading import session
from .courses_dialog import CoursesDialog
from . import theme
from .theme import label
from .students_view import StudentsView
from .template_editor import TemplateEditorView
from .scan_view import ScanView, LLMGradeView, STEP_UPLOAD, STEP_NAMES
from .hand_grade_view import HandGradeView
from .review_view import ReviewView
from .return_view import ReturnView
from .settings_view import SettingsView
from .help_view import HelpDialog

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LMGrader")
        self.resize(1400, 880)
        self.setMinimumSize(1100, 700)
        # gradings cut off by a crash / power loss go back to "ready"; saved questions are kept
        self.recovered = session.recover_interrupted()
        self._help = None        # the help window, created on first use

        self.students_view = StudentsView()
        self.template_view = TemplateEditorView()
        self.scan_view = ScanView(self.template_view, self.students_view, steps=(STEP_UPLOAD, STEP_NAMES))
        self.hand_view = HandGradeView(self.template_view)
        self.llm_view = LLMGradeView(self.template_view, self.students_view)
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
        # the course every tab works in (its roster and exams)
        sl.addWidget(label("COURSE", "courseLabel"))
        self.cb_course = QComboBox()
        self.cb_course.setObjectName("coursePicker")
        self.cb_course.setToolTip("Every tab shows this course's students and exams")
        sl.addWidget(self.cb_course)
        self.btn_courses = QPushButton("Manage courses…")
        self.btn_courses.setObjectName("courseManage")
        self.btn_courses.setCursor(Qt.CursorShape.PointingHandCursor)
        sl.addWidget(self.btn_courses)
        self._fill_courses()
        self.cb_course.activated.connect(lambda _: self.switch_course(self.cb_course.currentData()))
        self.btn_courses.clicked.connect(self.manage_courses)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(0, 0))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sl.addWidget(self.nav, 1)
        self.btn_help = QPushButton("?  Help")
        self.btn_help.setObjectName("sidebarBtn")
        self.btn_help.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_help.setToolTip("Help for this tab (F1)")
        self.btn_help.clicked.connect(self.show_help)
        sl.addWidget(self.btn_help)
        QShortcut(QKeySequence(QKeySequence.StandardKey.HelpContents), self, activated=self.show_help)
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
        self.tab_topics = []     # help topic (help_view.TOPICS key) of each tab, by stack index
        for text, view, topic in [("Exams & Templates", self.template_view, "templates"),
                                  ("Students", self.students_view, "students"),
                                  ("Scan & Names", self.scan_view, "scan"), ("Hand Grade", self.hand_view, "hand"),
                                  ("LLM Grade", self.llm_view, "llm"), ("Review", self.review_view, "review"),
                                  ("Return", self.return_view, "return"), ("Settings", self.settings_view, "settings")]:
            self.nav.addItem(QListWidgetItem(text))
            self.stack.addWidget(view)
            self.tab_topics.append(topic)
        # exam + version (template id) the tabs show: switching tabs keeps them
        self.tab_context = (None, None)
        self.nav.currentRowChanged.connect(self._on_nav)
        self.nav.setCurrentRow(0)
        self.apply_llm_mode()

        self.setCentralWidget(root)
        if self.recovered:
            self.statusBar().showMessage(f"Recovered {self.recovered} interrupted grading"
                                         f"{'s' if self.recovered != 1 else ''} — they will resume "
                                         "from the last saved question (LLM Grade).")
        else:
            self.statusBar().showMessage("Ready")

    # ---------- courses ----------
    def _fill_courses(self):
        cur = current_course()
        self.cb_course.blockSignals(True)
        self.cb_course.clear()
        for c in list_courses():
            self.cb_course.addItem(c["name"], c["id"])
        self.cb_course.setCurrentIndex(max(0, self.cb_course.findData(cur)))
        self.cb_course.blockSignals(False)
        self.setWindowTitle(f"LMGrader — {course_name(cur)}")

    def busy_job(self):
        """What is still running in the background (the course can't change meanwhile), or None."""
        jobs = [(self.scan_view.worker, "Scan & Names is still loading scans or reading names"),
                (self.llm_view.worker, "LLM Grade is still grading"),
                (self.hand_view.worker, "Hand Grade is still preparing answers"),
                (self.template_view.worker, "The LLM is still working in Exams & Templates"),
                (self.return_view.worker, "Return is still sending or uploading tests"),
                (self.review_view.retry_worker, "Review is still re-grading a question"),
                (self.review_view.export_worker, "Review is still exporting feedback PDFs")]
        return next((text for w, text in jobs if w is not None), None)

    def switch_course(self, course_id) -> bool:
        """Show another course in every tab. Refused while a job runs (it works on this course)."""
        if course_id is None or course_id == current_course():
            self._fill_courses()
            return course_id is not None
        job = self.busy_job()
        if job:
            QMessageBox.information(self, "Switch course", f"{job}. Stop it (or let it finish) before "
                                                           "switching course.")
            self._fill_courses()
            return False
        self.template_view.flush()          # pending edits are saved to the course they belong to
        self.review_view._flush_cards()     # feedback being typed in Review
        self._apply_course(course_id)
        return True

    def _apply_course(self, course_id):
        set_current_course(course_id)
        UI_CONFIG["course_id"] = current_course()
        save_settings()
        self.tab_context = (None, None)
        self._fill_courses()
        self.template_view.course_changed()
        self.students_view.refresh()
        w = self.stack.currentWidget()
        if w is not self.template_view and hasattr(w, "refresh"):
            w.refresh()                     # the other tabs refresh when they are opened
        self.statusBar().showMessage(f"Course: {course_name(current_course())}", 5000)

    def manage_courses(self):
        before = current_course()
        dlg = CoursesDialog(self, before)
        dlg.exec()
        if dlg.created is not None:          # a new course: open it (to import its roster)
            self.switch_course(dlg.created)
        elif current_course() != before:     # the current (empty) course was deleted
            self._apply_course(current_course())
        else:
            self._fill_courses()

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
        if self._help is not None:              # its key / "Next" colors come from the theme
            self._help.show_topic(self._help.current_topic())

    def apply_llm_mode(self):
        """Settings → "Use an LLM": off hides the LLM Grade tab and every AI action."""
        on = llm_enabled()
        if not on:                         # nothing may keep talking to an LLM
            self.llm_view.stop()
            if self.template_view.worker is not None:
                self.template_view.stop_ai()
        self.nav.setRowHidden(self.stack.indexOf(self.llm_view), not on)
        self.template_view.apply_llm_mode()
        self.scan_view.refresh()
        if self._help is not None:
            self._help.set_llm(on)

    # ---------- help ----------
    def current_topic(self) -> str:
        """Help topic of the tab on screen."""
        i = self.stack.currentIndex()
        return self.tab_topics[i] if 0 <= i < len(self.tab_topics) else "start"

    def show_help(self):
        """Help button / F1: the help window, on the topic of the tab on screen. It stays open
        (non-modal) and follows the tabs."""
        if self._help is None:
            self._help = HelpDialog(self)
        self._help.set_llm(llm_enabled())
        self._help.show_topic(self.current_topic())
        self._help.show(); self._help.raise_(); self._help.activateWindow()

    def _remember_context(self, view):
        """Keep the exam/version `view` shows. A view without a version (e.g. Scan & Names on
        Auto-detect) keeps the version already remembered for that exam."""
        if not hasattr(view, "context"):
            return
        exam, tid = view.context()
        if not exam:
            return
        if tid is None and exam == self.tab_context[0]:
            tid = self.tab_context[1]
        self.tab_context = (exam, tid)

    def _on_nav(self, row):
        self._remember_context(self.stack.currentWidget())
        self.stack.setCurrentIndex(row)
        w = self.stack.widget(row)
        if w is not self.template_view:
            self.template_view.flush()    # other pages read the template from the database
        if hasattr(w, "set_context") and self.tab_context[0]:
            w.set_context(*self.tab_context)   # applied by the refresh below (the editor: right away)
        if w is self.review_view:
            self.review_view.refresh()
        elif w in (self.scan_view, self.llm_view, self.hand_view):
            w.refresh()
        elif w is self.return_view:
            self.return_view.refresh()
        if self._help is not None and self._help.isVisible():   # the open help follows the tab
            self._help.show_topic(self.current_topic())

    def closeEvent(self, e):
        tv = self.template_view
        if tv.dirty and not tv._confirm_discard():
            e.ignore()
            return
        self.scan_view.shutdown()   # unfinished work is persisted and resumes next start
        self.llm_view.shutdown()
        self.hand_view.shutdown()
        self.return_view.shutdown()  # sent emails are recorded; re-running skips them
        super().closeEvent(e)
