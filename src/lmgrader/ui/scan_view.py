"""Persisted test queue: Upload -> Names -> (LLM) Grade.

The app shows it as two pages: "Scan & Names" (ScanView with the upload and names steps) and
"LLM Grade" (LLMGradeView, the grade step); hand grading has its own page (hand_grade_view).
ScanView() with no steps argument still has all three steps.

Everything shown here is read from the database (`submissions` table), and every action is
written back immediately. Closing the app, a crash or a power cut never loses work:
uploaded tests, identified/confirmed students and every graded question survive, and
grading resumes at the first question without a saved grade.
"""
import time
import json
import os
from typing import Dict, List, Optional
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QFileDialog, QMessageBox,
                             QProgressBar, QPlainTextEdit, QTableWidget, QTableWidgetItem, QCheckBox,
                             QSplitter, QHeaderView, QAbstractItemView, QStackedWidget, QButtonGroup,
                             QFrame, QLabel)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPixmap

from ..db import (list_exams, list_versions, list_students, list_submissions_by_status, get_submission,
                  delete_submission, list_grades, add_submission, update_submission, list_returns)
from ..models import (Submission, SUB_NEW, SUB_IDENTIFIED, SUB_READY, SUB_GRADING, SUB_GRADED, SUB_ERROR)
from ..scanner import get_scanner
from ..scanner.image_input import import_pages, IMAGE_FILTER
from ..grading import Grader, session
from ..grading.identify import crop_paths
from ..students.matching import normalize_id
from ..template.alignment import read_image
from ..llm import LLMClient
from ..config import data_dir, NAMES_CONFIG, llm_enabled
from ..students import trocr
from . import theme
from .theme import button, card, label, setup_page
from .widgets import PageViewer, Overlay, Worker

STEP_UPLOAD, STEP_NAMES, STEP_GRADE = 0, 1, 2
# names table columns
(COL_N_NUM, COL_N_NAME_IMG, COL_N_ID_IMG, COL_N_NAME, COL_N_ID, COL_N_VERSION, COL_N_STUDENT,
 COL_N_STATUS) = range(8)

STATUS_TEXT = {SUB_NEW: "Not read yet",SUB_IDENTIFIED: "Needs you", SUB_READY: "Ready", SUB_GRADING: "Grading…",
               SUB_GRADED: "Graded", SUB_ERROR: "Error"}
STATUS_COLOR = {SUB_NEW: "MUTED", SUB_IDENTIFIED: "WARN", SUB_READY: "OK",       # theme color names
                SUB_GRADING: "ACCENT", SUB_GRADED: "OK", SUB_ERROR: "DANGER"}   # (light/dark)

def needs_human(sub: Submission) -> bool:
    return sub.status == SUB_IDENTIFIED or (sub.status == SUB_ERROR and not sub.template_id)

def name_crop_path(sub_id: int) -> str:
    return crop_paths(sub_id)[0]

def id_crop_path(sub_id: int) -> str:
    return crop_paths(sub_id)[1]

# ---- pending pages: scanned/uploaded pages that don't fill a whole test yet (persisted) ----
def _pending_file():
    return data_dir() / "pending_pages.json"

def load_pending(exam: str) -> List[dict]:
    try:
        return json.loads(_pending_file().read_text(encoding="utf-8")).get(exam, [])
    except (OSError, ValueError):
        return []

def save_pending(exam: str, pages: List[dict]):
    try:
        allp = json.loads(_pending_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        allp = {}
    allp[exam] = pages
    _pending_file().write_text(json.dumps(allp, indent=1), encoding="utf-8")

def describe_pages(entries: List[dict]) -> str:
    """'batch.pdf p3–4, extra.png' from page entries {path, file, page}."""
    parts, cur = [], None
    for e in entries:
        f, p = e.get("file", "?"), e.get("page", 1)
        if cur and cur[0] == f and p == cur[2] + 1:
            cur[2] = p
        else:
            if cur:
                parts.append(cur)
            cur = [f, p, p]
    if cur:
        parts.append(cur)
    return ", ".join(f"{f} p{a}" + (f"–{b}" if b != a else "") for f, a, b in parts)

def unassigned_students(students, subs, sub):
    """Students the dropdown of test `sub` offers: its own student + those on no other test."""
    taken = {s.student_id for s in subs if s.student_id is not None and s.status != SUB_ERROR}
    return [s for s in students if s.id not in taken or s.id == sub.student_id]

# ---- worker jobs (background threads; every result is persisted before it is reported) ----
def job_import(files, worker=None):
    """Copy the chosen scans into the data dir (every PDF page rendered at 300 dpi), reporting
    each page. Returns the page entries {path, file, page}. Stopped: the pages already written
    are deleted and nothing is added."""
    entries = []
    for fi, f in enumerate(files):
        name = os.path.basename(f)
        what = name + (f" (file {fi + 1} of {len(files)})" if len(files) > 1 else "")
        worker.progress.emit(0, 0, f"Opening {what}…")
        report = lambda done, total, what=what: worker.progress.emit(
            done, total, f"Loading {what}… page {min(done + 1, total)} of {total}")
        pages = import_pages(f, progress=report, should_stop=lambda: worker.cancelled)
        entries += [{"path": p, "file": name, "page": k} for k, p in enumerate(pages, start=1)]
        if worker.cancelled:
            break
    if worker.cancelled:
        for e in entries:
            try:
                os.remove(e["path"])
            except OSError:
                pass
        return []
    return entries

def job_identify(sub_ids, versions, students, use_llm, worker=None):
    """Version + student of every test. Names/IDs are read by TrOCR (local) first; the LLM is
    asked only when TrOCR's match is unsure (and reads the version field), if use_llm and the
    server answers."""
    ocr = None
    if students:
        worker.progress.emit(0, len(sub_ids), "Loading the handwriting model (TrOCR)… the first time it "
                                              "is downloaded (~1.3 GB)")
        ocr = trocr.get_reader()
        if ocr is None:
            worker.item.emit(("log", "TrOCR is not available (torch/transformers missing or the model could "
                                     "not be loaded): names are read by the LLM only."))
    llm = LLMClient.from_config() if use_llm else None
    if llm is not None and hasattr(llm, "reachable") and not llm.reachable():
        worker.item.emit(("log", "The LLM server does not answer: names are read with TrOCR only "
                                 "(unsure matches wait for you)."))
        llm = None
    worker.llm = llm                      # lets Stop abort the in-flight request
    for n, sid in enumerate(sub_ids):
        if worker.cancelled:
            break
        worker.progress.emit(n, len(sub_ids), f"Reading IDs / names… test {n + 1} of {len(sub_ids)}")
        sub = get_submission(sid)
        if sub is None or sub.status in (SUB_GRADING, SUB_GRADED):
            continue
        original = get_submission(sid)
        try:
            session.identify_submission(sub, versions, students, llm, ocr=ocr)
        except Exception as e:
            sub.status, sub.error = SUB_ERROR, str(e)
            update_submission(sub)
        if worker.cancelled:
            # stopped while this test was being read: leave it exactly as it was ('new' stays 'new')
            update_submission(original)
            break
        worker.item.emit(("identified", sid))
    worker.progress.emit(len(sub_ids), len(sub_ids), "Stopped" if worker.cancelled else "Names read")

def job_grade(sub_ids, worker=None):
    grader = Grader(LLMClient.for_grading())
    worker.llm = grader.llm
    for n, sid in enumerate(sub_ids):
        if worker.cancelled:
            break
        sub = get_submission(sid)
        if sub is None or sub.status == SUB_GRADED:
            continue
        worker.progress.emit(n, len(sub_ids), f"Grading test {n + 1} of {len(sub_ids)}")
        worker.item.emit(("started", sid))
        session.grade_one(grader, sub,
                          progress_cb=lambda i, t, sid=sid: worker.item.emit(("region", sid, i, t)),
                          should_stop=lambda: worker.cancelled)
        worker.item.emit(("graded", sid))
    worker.progress.emit(len(sub_ids), len(sub_ids), "Stopped" if worker.cancelled else "Grading finished")

def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    hh = t.horizontalHeader()
    for c in range(len(headers)):
        hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
    return t

def _status_item(sub: Submission, text: str = None, color: str = None):
    """color: a theme color name instead of the status's own."""
    it = QTableWidgetItem(text or STATUS_TEXT.get(sub.status, sub.status))
    it.setForeground(QColor(getattr(theme, color or STATUS_COLOR.get(sub.status, "TEXT"))))
    f = it.font(); f.setBold(True); it.setFont(f)
    if sub.error:
        it.setToolTip(sub.error)
    return it

ALL_STEPS = (STEP_UPLOAD, STEP_NAMES, STEP_GRADE)
_TITLES = {
    ALL_STEPS: ("Scan & Grade", "Upload scans → confirm who wrote each test → grade. "
                                "Progress is saved continuously; you can close the app at any time."),
    (STEP_UPLOAD, STEP_NAMES): ("Scan & Names", "Upload the scanned tests, then confirm who wrote each one. "
                                                "Names are read on this computer (TrOCR); everything is saved "
                                                "as you go."),
    (STEP_GRADE,): ("LLM Grade", "The AI grades every question that nobody graded by hand. Questions you "
                                 "graded in Hand Grade are never touched. Progress is saved per question."),
}

class ScanView(QWidget):
    def __init__(self, template_view=None, students_view=None, steps=ALL_STEPS):
        super().__init__()
        self.steps = tuple(steps)
        title, subtitle = _TITLES.get(self.steps, _TITLES[ALL_STEPS])
        body, actions = setup_page(self, title, subtitle)
        self.scanner = get_scanner()
        self.worker: Optional[Worker] = None
        self.subs: List[Submission] = []
        self.students = []
        self.versions = []
        self.templates: Dict[int, object] = {}
        self.preview_id: Optional[int] = None
        self._region_progress = ""
        self._tick = time.monotonic()

        # ---- exam / options bar ----
        bar_card, bar = card(QHBoxLayout, margins=10)
        bar.addWidget(label("Exam"))
        self.cb_exam = QComboBox(); self.cb_exam.setMinimumWidth(200)
        bar.addWidget(self.cb_exam)
        bar.addSpacing(10)
        bar.addWidget(label("Version"))
        self.cb_version = QComboBox(); self.cb_version.setMinimumWidth(120)
        self.cb_version.setToolTip("Auto-detect compares each scan with every version of the exam")
        bar.addWidget(self.cb_version)
        bar.addSpacing(10)
        self.chk_names = QCheckBox("Use the LLM when unsure")
        self.chk_names.setChecked(bool(NAMES_CONFIG.get("llm_fallback", True)))
        self.chk_names.setToolTip("Names and student IDs are always read on this computer first (TrOCR). "
                                  "When that match is not sure, ask the LLM too (it also reads the version "
                                  "field). Skipped automatically when the LLM server is not running.")
        self.chk_names.setVisible(STEP_NAMES in self.steps)
        bar.addWidget(self.chk_names)
        bar.addStretch()
        self.lbl_pages = label("", "muted")
        bar.addWidget(self.lbl_pages)
        body.addWidget(bar_card)

        # ---- resume banner ----
        self.banner = QFrame(); self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner); bl.setContentsMargins(12, 8, 12, 8)
        self.lbl_banner = QLabel("")
        bl.addWidget(self.lbl_banner, 1)
        self.btn_resume = button("Resume grading", "primary")
        bl.addWidget(self.btn_resume)
        self.banner.setVisible(False)
        body.addWidget(self.banner)

        # ---- tests added but not read yet: names are read only when asked ----
        self.names_banner = QFrame(); self.names_banner.setObjectName("banner")
        nl = QHBoxLayout(self.names_banner); nl.setContentsMargins(12, 8, 12, 8)
        self.lbl_names_banner = QLabel("")
        self.lbl_names_banner.setWordWrap(True)
        nl.addWidget(self.lbl_names_banner, 1)
        self.btn_read_names = button("Read names", "primary",
                                     "Detect the version of every new test and read its name and student ID")
        nl.addWidget(self.btn_read_names)
        self.names_banner.setVisible(False)
        body.addWidget(self.names_banner)

        # ---- step selector ----
        steps = QHBoxLayout()
        self.step_group = QButtonGroup(self)
        self.step_btns = []
        for i in range(3):
            b = button("")
            b.setCheckable(True)
            b.setMinimumHeight(38)
            b.setVisible(i in self.steps and len(self.steps) > 1)
            self.step_group.addButton(b, i)
            self.step_btns.append(b)
            steps.addWidget(b, 1)
            if i < 2:
                arrow = label("→", "muted")
                arrow.setVisible(i in self.steps and i + 1 in self.steps)
                steps.addWidget(arrow)
        body.addLayout(steps)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_upload())
        self.stack.addWidget(self._build_names())
        self.stack.addWidget(self._build_grade())
        split.addWidget(self.stack)

        pcard, pl = card(margins=10)
        prow = QHBoxLayout()
        prow.addWidget(label("Preview", "sectionTitle"))
        prow.addStretch()
        self.cb_page = QComboBox()
        prow.addWidget(self.cb_page)
        pl.addLayout(prow)
        self.lbl_preview_info = label("", "muted")
        pl.addWidget(self.lbl_preview_info)
        self.preview = PageViewer()
        self.preview.canvas.draw_enabled = False
        self.preview.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        pl.addWidget(self.preview, 1)
        split.addWidget(pcard)
        split.setSizes([760, 420])
        body.addWidget(split, 1)

        # ---- progress / log ----
        prog = QHBoxLayout()
        self.progress = QProgressBar(); self.progress.setVisible(False)
        self.lbl_progress = label("", "muted")
        self.btn_stop_job = button("Stop", "danger",
                                   "Stop the LLM now. Everything already finished is saved; "
                                   "the rest continues when you start it again.")
        self.btn_stop_job.setVisible(False)
        prog.addWidget(self.lbl_progress); prog.addWidget(self.progress, 1); prog.addWidget(self.btn_stop_job)
        body.addLayout(prog)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(80)
        self.log.setPlaceholderText("Activity log")
        body.addWidget(self.log)

        # ---- signals ----
        self.btn_stop_job.clicked.connect(self.stop)
        self.cb_exam.currentIndexChanged.connect(lambda _: self.refresh(reload_exams=False))
        self.step_group.idClicked.connect(self._go_step)
        self.btn_resume.clicked.connect(self._resume)
        self.btn_read_names.clicked.connect(self.read_names)
        self.cb_page.currentIndexChanged.connect(lambda _: self._show_preview_page())
        self.chk_names.toggled.connect(self._llm_fallback_toggled)
        self.stack.setCurrentIndex(self.steps[0])
        self.step_btns[self.steps[0]].setChecked(True)
        self.refresh()

    def _llm_fallback_toggled(self, on: bool):
        from ..config import save_settings
        NAMES_CONFIG["llm_fallback"] = bool(on)
        save_settings()
        self._update_steps()

    def _go_step(self, step):
        self.stack.setCurrentIndex(step)
        self.step_btns[step].setChecked(True)
        t = self._current_table()
        if self.preview_id is not None:
            for r in range(t.rowCount()):
                if t.item(r, 0).data(Qt.ItemDataRole.UserRole) == self.preview_id:
                    t.blockSignals(True); t.selectRow(r); t.blockSignals(False)
                    return
        self.preview_id = None
        self._auto_select()

    # ---------- step pages ----------
    def _build_upload(self):
        w, lay = card(margins=12)
        row = QHBoxLayout()
        self.btn_add = button("Add files…", "primary",
                              "Images or PDFs. Several files are joined in the order selected, "
                              "then cut into tests of the template's length.")
        self.btn_scan = button("Scan page")
        row.addWidget(self.btn_add); row.addWidget(self.btn_scan)
        row.addStretch()
        self.btn_reidentify = button("Re-read names", tooltip="Run version detection + name reading again")
        self.btn_select_all = button("Select all", tooltip="Select every test (Ctrl+A), e.g. to delete them all")
        self.btn_delete = button("Delete", "danger", "Delete the selected tests and their grades")
        row.addWidget(self.btn_reidentify); row.addWidget(self.btn_select_all); row.addWidget(self.btn_delete)
        lay.addLayout(row)

        self.pending_box = QFrame(); self.pending_box.setObjectName("banner")
        pl = QHBoxLayout(self.pending_box); pl.setContentsMargins(10, 6, 10, 6)
        self.lbl_pending = QLabel(""); self.lbl_pending.setWordWrap(True)
        pl.addWidget(self.lbl_pending, 1)
        self.btn_discard_pending = button("Discard pages")
        pl.addWidget(self.btn_discard_pending)
        self.pending_box.setVisible(False)
        lay.addWidget(self.pending_box)

        self.table_upload = _table(["#", "Source", "Pages", "Version", "Status"])
        self.table_upload.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table_upload, 1)
        self.lbl_empty = label("No tests yet. Add the scanned PDF(s) — one long file with every student's test "
                               "is fine; it is cut into tests automatically.", "muted")
        self.lbl_empty.setWordWrap(True)
        lay.addWidget(self.lbl_empty)

        self.btn_add.clicked.connect(self.do_import)
        self.btn_scan.clicked.connect(self.do_scan)
        self.btn_reidentify.clicked.connect(lambda: self.identify(self._selected_ids(self.table_upload), force=True))
        self.btn_select_all.clicked.connect(self.table_upload.selectAll)
        self.btn_delete.clicked.connect(lambda: self.delete_selected(self.table_upload))
        self.btn_discard_pending.clicked.connect(self._discard_pending)
        self.table_upload.itemSelectionChanged.connect(lambda: self._select_from(self.table_upload))
        return w

    def _build_names(self):
        w, lay = card(margins=12)
        row = QHBoxLayout()
        self.chk_only_me = QCheckBox("Only tests that need me")
        row.addWidget(self.chk_only_me)
        row.addStretch()
        self.btn_reidentify2 = button("Re-read selected")
        self.btn_confirm_all = button("Confirm all suggested", "primary",
                                      "Accept the suggested student for every test that has one")
        row.addWidget(self.btn_reidentify2); row.addWidget(self.btn_confirm_all)
        lay.addLayout(row)
        self.table_names = _table(["#", "Name field", "ID field", "Name read", "ID read", "Version", "Student",
                                   "Status"])
        self.table_names.horizontalHeader().setSectionResizeMode(COL_N_STUDENT, QHeaderView.ResizeMode.Stretch)
        self.table_names.horizontalHeader().setSectionResizeMode(COL_N_VERSION, QHeaderView.ResizeMode.Fixed)
        self.table_names.setColumnWidth(COL_N_VERSION, 96)    # holds the version dropdown
        self.table_names.verticalHeader().setDefaultSectionSize(54)
        lay.addWidget(self.table_names, 1)
        self.chk_only_me.toggled.connect(lambda _: self._render_names())
        self.btn_confirm_all.clicked.connect(self.confirm_all)
        self.btn_reidentify2.clicked.connect(lambda: self.identify(self._selected_ids(self.table_names), force=True))
        self.table_names.itemSelectionChanged.connect(lambda: self._select_from(self.table_names))
        return w

    def _build_grade(self):
        w, lay = card(margins=12)
        row = QHBoxLayout()
        self.btn_grade = button("Grade all ready", "primary")
        self.btn_stop = button("Stop", "danger", "Stops immediately; finished questions are kept and "
                                                 "grading resumes from the next question")
        self.btn_regrade = button("Regrade…", tooltip="Grade the selected tests again from scratch (all graded "
                                                      "tests if none is selected), e.g. after changing the "
                                                      "answer key, notes or strictness")
        self.chk_unconfirmed = QCheckBox("Also grade tests without a confirmed student")
        row.addWidget(self.btn_grade); row.addWidget(self.btn_stop); row.addWidget(self.btn_regrade)
        row.addWidget(self.chk_unconfirmed)
        row.addStretch()
        self.lbl_grade_summary = label("", "muted")
        row.addWidget(self.lbl_grade_summary)
        lay.addLayout(row)
        self.table_grade = _table(["#", "Student", "Version", "Score", "Status"])
        self.table_grade.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table_grade, 1)
        self.btn_grade.clicked.connect(self.grade_all)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_regrade.clicked.connect(lambda: self.regrade())
        self.table_grade.itemSelectionChanged.connect(lambda: self._select_from(self.table_grade))
        return w

    # ---------- data ----------
    @property
    def exam(self) -> str:
        return self.cb_exam.currentText()

    def pages_per_test(self) -> int:
        """Template pages per test (without blank/back pages)."""
        counts = {v.page_count for v in self.versions}
        return max(counts) if counts else 1

    def layout(self):
        """Scanned-page layout of a test (template page index or BLANK), from the first version."""
        return self.versions[0].layout if self.versions else [0]

    def scan_pages_per_test(self) -> int:
        return len(self.layout())

    def _layout_text(self) -> str:
        n, total = self.pages_per_test(), self.scan_pages_per_test()
        blanks = total - n
        txt = f"{total} scanned page{'s' if total != 1 else ''} per test"
        if blanks:
            txt += f" ({n} page{'s' if n != 1 else ''} + {blanks} back{'s' if blanks != 1 else ''})"
        return txt

    def context(self):
        """(exam, version id) for the other tabs. The version picker here only forces detection;
        "Auto-detect" gives None (the other tabs keep their version)."""
        return self.exam or None, self.cb_version.currentData()

    def set_context(self, exam, tid=None):
        """Show this exam at the next refresh (not while a job runs). The version picker is left
        alone: choosing a version here forces it on every scan read afterwards."""
        if exam and self.worker is None:
            self._want_exam = exam

    def refresh(self, reload_exams=True):
        if reload_exams:
            want, self._want_exam = getattr(self, "_want_exam", None), None
            exams = list_exams()
            cur = want if want in exams else self.exam
            self.cb_exam.blockSignals(True)
            self.cb_exam.clear()
            self.cb_exam.addItems(exams)
            if cur and self.cb_exam.findText(cur) >= 0:
                self.cb_exam.setCurrentIndex(self.cb_exam.findText(cur))
            self.cb_exam.blockSignals(False)
        cur_v = self.cb_version.currentData()
        self.versions = list_versions(self.exam) if self.exam else []
        self.templates = {v.id: v for v in self.versions}
        self.cb_version.blockSignals(True)
        self.cb_version.clear()
        self.cb_version.addItem("Auto-detect", None)
        for v in self.versions:
            self.cb_version.addItem(f"Version {v.version_label}", v.id)
        self.cb_version.setCurrentIndex(max(0, self.cb_version.findData(cur_v)))
        self.cb_version.blockSignals(False)
        counts = {v.scan_page_count for v in self.versions}
        if len(counts) > 1:
            self.lbl_pages.setText("⚠ versions have different scanned page counts")
            self.lbl_pages.setStyleSheet(f"color: {theme.WARN};")
        else:
            self.lbl_pages.setText(self._layout_text() if self.versions else "")
            self.lbl_pages.setStyleSheet("")
        self.lbl_pages.setToolTip("Set in Exams & Templates (e.g. “Scans include back sides”).")
        self.students = list_students()
        self.chk_names.setVisible(STEP_NAMES in self.steps and llm_enabled())
        self.reload()     # new tests wait for "Read names" (never read automatically)

    def reload(self):
        """Re-read the queue from the database and redraw everything."""
        self.subs = list_submissions_by_status(self.exam) if self.exam else []
        self._grades = {s.id: list_grades(s.id) for s in self.subs
                        if s.status in (SUB_GRADED, SUB_GRADING, SUB_READY, SUB_ERROR)}
        self._render_upload()
        self._render_names()
        self._render_grade()
        self._render_pending()
        self._update_steps()
        self._update_enabled()
        if self.preview_id and not any(s.id == self.preview_id for s in self.subs):
            self.preview_id = None
            self._show_preview_page()
        self._auto_select()

    def _current_table(self):
        return (self.table_upload, self.table_names, self.table_grade)[self.stack.currentIndex()]

    def _auto_select(self):
        """Keep the preview useful: show the first test when nothing is selected."""
        t = self._current_table()
        if self.preview_id is None and t.rowCount():
            t.selectRow(0)

    def _sub(self, sid) -> Optional[Submission]:
        return next((s for s in self.subs if s.id == sid), None)

    def _num(self, sub) -> int:
        return next((i + 1 for i, s in enumerate(self.subs) if s.id == sub.id), 0)

    def _version_text(self, sub):
        t = self.templates.get(sub.template_id)
        return f"v{t.version_label}" if t else "—"

    def _student_name(self, sid):
        """Name of a test's student (any course: an exam moved here keeps its students)."""
        name = next((s.name for s in self.students if s.id == sid), "")
        if not name and sid:
            name = next((s.name for s in list_students(all_courses=True) if s.id == sid), "")
        return name

    # ---------- rendering ----------
    def _id_item(self, sub):
        it = QTableWidgetItem(str(self._num(sub)))
        it.setData(Qt.ItemDataRole.UserRole, sub.id)
        return it

    def _render_upload(self):
        t = self.table_upload
        t.setRowCount(0)
        for sub in self.subs:
            r = t.rowCount(); t.insertRow(r)
            t.setItem(r, 0, self._id_item(sub))
            t.setItem(r, 1, QTableWidgetItem(sub.source or os.path.basename(sub.pages[0] if sub.pages else "")))
            pages = f"{len(sub.pages)}" + (f" + {sum(1 for b in sub.backs if b)} back" if any(sub.backs) else "")
            t.setItem(r, 2, QTableWidgetItem(pages))
            t.setItem(r, 3, QTableWidgetItem(self._version_text(sub)))
            t.setItem(r, 4, _status_item(sub))
        self.lbl_empty.setVisible(not self.subs)

    def _duplicates(self):
        seen, dup = set(), set()
        for s in self.subs:
            if s.student_id is not None and s.status != SUB_ERROR:
                (dup if s.student_id in seen else seen).add(s.student_id)
        return dup

    def _render_names(self):
        t = self.table_names
        t.setRowCount(0)
        dup = self._duplicates()
        subs = sorted(self.subs, key=lambda s: (not needs_human(s), s.id))
        if self.chk_only_me.isChecked():
            subs = [s for s in subs if needs_human(s)]
        roster_ids = {s.id: normalize_id(s.student_id) for s in self.students}
        for sub in subs:
            r = t.rowCount(); t.insertRow(r)
            t.setItem(r, COL_N_NUM, self._id_item(sub))
            tmpl = self.templates.get(sub.template_id)
            for col, path, has_field, missing in (
                    (COL_N_NAME_IMG, name_crop_path(sub.id), bool(tmpl and tmpl.name_rect), "(no name field)"),
                    (COL_N_ID_IMG, id_crop_path(sub.id), bool(tmpl and tmpl.id_rect), "(no ID field)")):
                thumb = QLabel()
                pm = QPixmap(path) if has_field and os.path.exists(path) else QPixmap()
                if not pm.isNull():
                    thumb.setPixmap(pm.scaled(200, 46, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
                else:
                    thumb.setText(f"  {missing}" if sub.template_id else "  …")
                    thumb.setStyleSheet(f"color: {theme.MUTED};")
                t.setCellWidget(r, col, thumb)
            read = QTableWidgetItem(sub.name_read or "—")
            if sub.name_read:
                read.setToolTip(f"Fuzzy match score {sub.name_match_score:.0f}")
            t.setItem(r, COL_N_NAME, read)
            id_ok = bool(sub.id_read) and sub.student_id is not None and roster_ids.get(sub.student_id) == sub.id_read
            id_item = QTableWidgetItem((("✓ " if id_ok else "") + sub.id_read) if sub.id_read else "—")
            if id_ok:
                id_item.setForeground(QColor(theme.OK))
                id_item.setToolTip("Student ID matches the roster exactly")
            elif sub.id_read:
                id_item.setToolTip("This ID is not on the roster — matched by name")
            t.setItem(r, COL_N_ID, id_item)
            t.setCellWidget(r, COL_N_VERSION, self._version_combo(sub))

            cb = QComboBox()
            cb.addItem("— choose student —", None)
            for s in unassigned_students(self.students, self.subs, sub):
                cb.addItem(s.name + (f"  ({s.student_id})" if s.student_id else ""), s.id)
            cb.setCurrentIndex(max(0, cb.findData(sub.student_id)))
            cb.setEnabled(self.worker is None and sub.status not in (SUB_GRADING, SUB_GRADED))
            # red: nobody on the roster matched (or two tests on one student); yellow: a suggestion
            # the reader is not sure of
            not_found = needs_human(sub) and sub.student_id is None
            if not_found or sub.student_id in dup:
                cb.setStyleSheet(f"QComboBox {{ border: 1px solid {theme.DANGER}; background: {theme.DANGER_SOFT}; }}")
            elif needs_human(sub):
                cb.setStyleSheet(f"QComboBox {{ border: 1px solid {theme.WARN}; background: {theme.WARN_SOFT}; }}")
            cb.setToolTip("Another test has the same student" if sub.student_id in dup else
                          "No student on the roster matched: choose who wrote this test" if not_found else
                          "Suggested — please confirm" if sub.student_id and not sub.name_confirmed else "")
            cb.currentIndexChanged.connect(lambda _, c=cb, sid=sub.id: self._student_chosen(sid, c.currentData()))
            t.setCellWidget(r, COL_N_STUDENT, cb)
            color = None
            if sub.status == SUB_IDENTIFIED and session.version_hold(sub):
                text = "Check version"
            elif sub.status == SUB_IDENTIFIED and sub.student_id:
                text = "Confirm"
            elif sub.status == SUB_IDENTIFIED:
                text, color = "No match", "DANGER"
            else:
                text = None
            t.setItem(r, COL_N_STATUS, _status_item(sub, text, color))

    def _version_combo(self, sub) -> QComboBox:
        """Per-test version picker; a manual choice sticks (re-reading never changes it)."""
        cb = QComboBox()
        if sub.template_id not in self.templates:
            cb.addItem("— choose —", None)
        for v in self.versions:
            cb.addItem(f"v{v.version_label}", v.id)
        cb.setCurrentIndex(max(0, cb.findData(sub.template_id)))
        cb.setEnabled(self.worker is None and sub.status != SUB_GRADING and len(self.versions) > 0)
        how = {"manual": "set by you", "layout": "detected from the page layout"}.get(
            sub.version_read, f"read from the version field ({sub.version_read[7:]})"
            if sub.version_read.startswith("field: ") else sub.version_read or "not detected yet")
        cb.setToolTip(f"Version {how}. Pick another to change it.")
        if session.version_hold(sub) or not sub.template_id:
            cb.setStyleSheet(f"QComboBox {{ border: 1px solid {theme.WARN}; background: {theme.WARN_SOFT}; }}")
        # activated (not currentIndexChanged): re-picking the detected version confirms it
        cb.activated.connect(lambda _, c=cb, sid=sub.id: self._version_chosen(sid, c.currentData()))
        return cb

    def _version_chosen(self, sub_id, template_id):
        sub, tmpl = get_submission(sub_id), self.templates.get(template_id)
        if sub is None or tmpl is None:
            return
        if sub.template_id == tmpl.id and sub.version_confirmed:
            return
        if sub.template_id != tmpl.id and list_grades(sub.id) and QMessageBox.question(
                self, "Change version",
                f"Test {self._num(sub)} already has grades for another version. Changing it to "
                f"v{tmpl.version_label} deletes them and the test is graded again. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            self.reload()          # puts the combo back
            return
        session.set_version(sub, tmpl)
        self.log.appendPlainText(f"Test {self._num(sub)}: version set to v{tmpl.version_label}.")
        self.reload()

    def _render_grade(self):
        t = self.table_grade
        t.setRowCount(0)
        n_ready = n_done = n_review = 0
        for sub in self.subs:
            grades = self._grades.get(sub.id, [])
            r = t.rowCount(); t.insertRow(r)
            t.setItem(r, 0, self._id_item(sub))
            name = self._student_name(sub.student_id)
            it = QTableWidgetItem(name or (f"unconfirmed — “{sub.name_read}”" if sub.name_read else "unassigned"))
            if not sub.name_confirmed:      # red: no student; yellow: not confirmed yet
                it.setForeground(QColor(theme.WARN if sub.student_id is not None else theme.DANGER))
            t.setItem(r, 1, it)
            t.setItem(r, 2, QTableWidgetItem(self._version_text(sub)))
            tmpl = self.templates.get(sub.template_id)
            if grades:
                mx = sum(rg.points for rg in tmpl.regions) if tmpl else sum(g.max_points for g in grades)
                score = f"{sum(g.score for g in grades):g} / {mx:g}"
                if sub.status != SUB_GRADED and tmpl:
                    score += f"  ({len(grades)}/{len(tmpl.regions)} questions)"
            else:
                score = "—"
            by_hand = sum(1 for g in grades if g.human_graded)
            if by_hand:
                score += f"  · {by_hand} by hand"
            t.setItem(r, 3, QTableWidgetItem(score))
            review = sub.status == SUB_GRADED and any(g.needs_human_review for g in grades)
            text = "Graded · review" if review else None
            t.setItem(r, 4, _status_item(sub, text))
            n_ready += sub.status == SUB_READY
            n_done += sub.status == SUB_GRADED
            n_review += review
        self.lbl_grade_summary.setText(f"{n_done} graded · {n_ready} ready · {n_review} need review")

    def _render_pending(self):
        pend = load_pending(self.exam) if self.exam else []
        if pend:
            need = self.scan_pages_per_test()
            self.lbl_pending.setText(
                f"⚠  {len(pend)} page(s) waiting — not enough for a full test of {need} page(s): "
                f"{describe_pages(pend)}. They will be combined with the next upload or scan.")
        self.pending_box.setVisible(bool(pend))

    def _update_steps(self):
        n = len(self.subs)
        need = sum(needs_human(s) for s in self.subs)
        waiting = sum(s.status == SUB_NEW for s in self.subs)
        ready = sum(s.status == SUB_READY for s in self.subs)
        graded = sum(s.status == SUB_GRADED for s in self.subs)
        self.step_btns[0].setText(f"1  Upload  ·  {n} test{'s' if n != 1 else ''}")
        kind = getattr(self.worker, "kind", "")
        if need:
            names = f"{need} need you"
        elif waiting:
            names = "reading…" if kind == "identify" else f"{waiting} to read"
        else:
            names = "all set"
        self.step_btns[1].setText(f"2  Names  ·  {names}")
        show_names = self.worker is None and waiting > 0 and STEP_NAMES in self.steps
        if show_names:
            how = ("on this computer (TrOCR), with the LLM when unsure" if llm_enabled() and
                   self.chk_names.isChecked() else "on this computer (TrOCR)")
            self.lbl_names_banner.setText(
                f"⏸  {waiting} test{'s' if waiting != 1 else ''} not read yet. Read names detects each "
                f"test's version and reads its name and student ID {how}.")
        self.names_banner.setVisible(show_names)
        self.step_btns[2].setText(f"3  Grade  ·  {graded}/{n} graded" + (f", {ready} ready" if ready else ""))
        partial = [s for s in self.subs if s.status == SUB_READY and self._grades.get(s.id)]
        show = self.worker is None and ready > 0 and STEP_GRADE in self.steps
        if show:
            msg = f"{ready} test{'s' if ready != 1 else ''} waiting to be graded"
            if partial:
                msg += f" ({len(partial)} partly graded — finished questions are kept)"
            self.lbl_banner.setText("⏸  " + msg + ".")
        self.banner.setVisible(show)

    def _update_enabled(self):
        busy = self.worker is not None
        has_exam = bool(self.versions)
        for w in (self.btn_add, self.btn_scan, self.btn_select_all, self.btn_delete, self.btn_reidentify,
                  self.btn_reidentify2, self.btn_read_names,
                  self.btn_confirm_all, self.btn_discard_pending, self.cb_exam, self.cb_version):
            w.setEnabled(not busy)
        loading = busy and getattr(self.worker, "kind", "") == "import"
        self.btn_add.setText("Loading…" if loading else "Add files…")
        self.btn_add.setEnabled(not busy and has_exam)
        self.btn_scan.setEnabled(not busy and has_exam)
        self.btn_grade.setEnabled(not busy and any(self._gradable()))
        self.btn_regrade.setEnabled(not busy and any(self._regradable(s) for s in self.subs))
        can_stop = busy and not self.worker.cancelled
        self.btn_stop.setEnabled(can_stop and getattr(self.worker, "kind", "") == "grade")
        self.btn_stop_job.setVisible(busy)
        self.btn_stop_job.setEnabled(can_stop)
        self.btn_resume.setEnabled(not busy)

    # ---------- preview ----------
    def _selected_ids(self, table) -> List[int]:
        rows = sorted({i.row() for i in table.selectedIndexes()})
        out = []
        for r in rows:
            it = table.item(r, 0)
            if it is not None:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _select_from(self, table):
        ids = self._selected_ids(table)
        if not ids or ids[0] == self.preview_id:
            return
        self.preview_id = ids[0]
        sub = self._sub(self.preview_id)
        self.cb_page.blockSignals(True)
        self.cb_page.clear()
        for p in range(len(sub.pages) if sub else 0):
            self.cb_page.addItem(f"Page {p + 1}", p)
        if sub and sub.template_id:
            t = self.templates.get(sub.template_id)
            if t and t.name_rect and table is self.table_names:
                self.cb_page.setCurrentIndex(min(t.name_page, self.cb_page.count() - 1))
        self.cb_page.blockSignals(False)
        self._show_preview_page()

    def _show_preview_page(self):
        sub = self._sub(self.preview_id) if self.preview_id else None
        if sub is None or not sub.pages:
            self.preview.set_image(None); self.preview.canvas.set_overlays([]); self.lbl_preview_info.setText("")
            return
        p = max(0, self.cb_page.currentIndex())
        aligned = sub.aligned_pages[p] if p < len(sub.aligned_pages) and sub.aligned_pages[p] else ""
        img = read_image(aligned) if aligned else None
        if img is None:
            img = read_image(sub.pages[p]) if p < len(sub.pages) else None
        self.preview.set_image(img)
        t = self.templates.get(sub.template_id)
        ovs = []
        if t and aligned:
            for r in t.regions:
                if r.page == p:
                    ovs.append(Overlay(r.rect, theme.COLOR_ANSWER, r.problem))
            if t.name_rect and t.name_page == p:
                ovs.append(Overlay(t.name_rect, theme.COLOR_NAME, "Name", selected=True))
            if t.id_rect and t.id_page == p:
                ovs.append(Overlay(t.id_rect, theme.COLOR_NAME, "ID", selected=True))
            if t.version_rect and t.version_page == p:
                ovs.append(Overlay(t.version_rect, theme.COLOR_NAME, "Version", selected=True))
        self.preview.canvas.set_overlays(ovs)
        info = f"Test {self._num(sub)} · {self._version_text(sub)}"
        info += " · aligned" if aligned else " · original scan"
        if sub.error:
            info += f" · {sub.error}"
        self.lbl_preview_info.setText(info)

    # ---------- step 1: upload ----------
    def do_import(self):
        if self.worker:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Add scanned tests", "", IMAGE_FILTER)
        if files:
            self.import_files(files)

    def import_files(self, files):
        """Load the files in the background (progress bar + page count; big PDFs take a while),
        then cut them into tests. Names are not read until "Read names"."""
        if self.worker or not files:
            return
        w = Worker(job_import, list(files))
        w.done.connect(self._imported)          # before start: a tiny file can finish at once
        self._start(w, "import")

    def _imported(self, entries):
        if entries:
            self.add_pages(entries)

    def do_scan(self):
        devs = self.scanner.list_devices()
        path = self.scanner.scan(device=devs[0] if devs else None)
        if not path:
            QMessageBox.warning(self, "Scan", "Hardware scan unavailable or failed. Use “Add files…” instead.")
            return
        n = len(load_pending(self.exam)) + 1
        self.add_pages([{"path": p, "file": "scanner", "page": n} for p in import_pages(path)])

    def add_pages(self, entries):
        """entries: page dicts {path, file, page} or plain paths. Joins them with pending pages,
        cuts them into tests of the template's length and persists one submission per test."""
        if not self.versions:
            QMessageBox.information(self, "Upload", "Create the exam in “Exams & Templates” first.")
            return []
        if len({v.scan_page_count for v in self.versions}) > 1:
            QMessageBox.warning(self, "Page count", "The versions of this exam have different scanned page "
                                                    f"counts; using {self.scan_pages_per_test()} pages per test "
                                                    f"(version {self.versions[0].version_label}).")
        norm = []
        for k, e in enumerate(entries, start=1):
            norm.append(e if isinstance(e, dict) else {"path": e, "file": os.path.basename(e), "page": 1})
        pending = load_pending(self.exam) + norm
        tests, leftover = session.split_by_layout(list(range(len(pending))), self.layout())
        new_ids = []
        for fronts, backs in tests:
            used = sorted(fronts + [b for b in backs if b != ""])
            sub = Submission(exam_name=self.exam, status=SUB_NEW,
                             scan_pages=[pending[i]["path"] for i in fronts],
                             back_pages=[pending[i]["path"] if i != "" else "" for i in backs],
                             source=describe_pages([pending[i] for i in used]))
            add_submission(sub)
            new_ids.append(sub.id)
        save_pending(self.exam, [pending[i] for i in leftover])
        msg = f"Added {len(tests)} test(s) from {len(norm)} page(s)"
        if leftover:
            msg += f"; {len(leftover)} page(s) left over ({describe_pages([pending[i] for i in leftover])})"
        if new_ids and STEP_NAMES in self.steps:
            msg += ". Press “Read names” when you are ready"
        self.log.appendPlainText(msg + ".")
        self.reload()
        return new_ids

    def _discard_pending(self):
        if QMessageBox.question(self, "Discard", "Discard the pages waiting for a full test?") \
                == QMessageBox.StandardButton.Yes:
            save_pending(self.exam, [])
            self._render_pending()

    def delete_selected(self, table):
        if self.worker:
            return
        ids = self._selected_ids(table)
        graded = sum(1 for s in ids if self._grades.get(s))
        msg = f"Delete {len(ids)} test(s) and their grades?"
        if graded:
            msg += f"\n\n{graded} of them already have grades — those are lost too. This cannot be undone."
        if not ids or QMessageBox.question(self, "Delete", msg) != QMessageBox.StandardButton.Yes:
            return
        for sid in ids:
            delete_submission(sid)
        self.log.appendPlainText(f"Deleted {len(ids)} test(s).")
        self.reload()

    # ---------- step 2: names ----------
    def read_names(self):
        """The "Read names" button: version + name + ID of every test not read yet."""
        self.identify([s.id for s in self.subs if s.status == SUB_NEW])

    def identify(self, sub_ids, force=False):
        if self.worker or not self.versions:
            return
        ids = [s for s in sub_ids if (sub := self._sub(s)) and sub.status not in (SUB_GRADING, SUB_GRADED)]
        if not ids:
            return
        vid = self.cb_version.currentData()
        versions = [v for v in self.versions if v.id == vid] if vid else self.versions
        use_llm = llm_enabled() and self.chk_names.isChecked()
        self._start(Worker(job_identify, ids, versions, self.students, use_llm), "identify")

    def _student_chosen(self, sub_id, student_id):
        sub = get_submission(sub_id)
        if sub is None:
            return
        session.confirm_student(sub, student_id)
        name = self._student_name(student_id) or "nobody"
        self.log.appendPlainText(f"Test {self._num(sub)}: student set to {name}.")
        self.reload()

    def confirm_all(self):
        n = 0
        for s in self.subs:
            if s.status == SUB_IDENTIFIED and s.student_id and not s.name_confirmed:
                session.confirm_student(s, s.student_id)
                n += 1
        self.log.appendPlainText(f"Confirmed {n} suggested student(s).")
        self.reload()

    # ---------- step 3: grade ----------
    def _gradable(self):
        for s in self.subs:
            if s.status == SUB_READY or (self.chk_unconfirmed.isChecked() and s.status == SUB_IDENTIFIED
                                         and s.template_id):
                yield s

    def grade_all(self):
        if self.worker:
            return
        todo = list(self._gradable())
        if not todo:
            return
        unconfirmed = sum(s.status == SUB_IDENTIFIED for s in self.subs)
        if unconfirmed and not self.chk_unconfirmed.isChecked():
            self.log.appendPlainText(f"{unconfirmed} test(s) skipped until their student is confirmed (step 2).")
        self._go_step(STEP_GRADE)
        self._start(Worker(job_grade, [s.id for s in todo]), "grade")

    def _regradable(self, sub) -> bool:
        """Graded, failed or partly graded tests (those with grades) can be graded again."""
        return bool(sub.template_id) and (sub.status in (SUB_GRADED, SUB_ERROR) or
                                          (sub.status == SUB_READY and bool(self._grades.get(sub.id))))

    def regrade(self, sub_ids=None, confirm=True, keep_human=True):
        """Grade tests again from scratch: their AI grades are deleted and they are graded now.
        sub_ids: the tests (default: the selected rows, or every graded test if none is selected).
        Questions graded by hand are kept unless the professor unticks that in the dialog."""
        if self.worker:
            return
        if sub_ids is None:
            sub_ids = self._selected_ids(self.table_grade) or [s.id for s in self.subs if s.status == SUB_GRADED]
        subs = [s for s in (self._sub(i) for i in sub_ids) if s is not None and self._regradable(s)]
        if not subs:
            QMessageBox.information(self, "Regrade", "Select graded tests to regrade.")
            return
        if confirm:
            n_human = sum(1 for s in subs for g in self._grades.get(s.id, []) if g.human_graded)
            returned = sum(1 for s in subs if any(r["status"] != "failed" for r in list_returns(s.id)))
            box = QMessageBox(self)
            box.setWindowTitle("Regrade")
            text = (f"Grade {len(subs)} test{'s' if len(subs) != 1 else ''} again with the AI?\n\n"
                    "Their current AI scores and feedback are replaced, using the current questions, "
                    "rubrics, notes, instructions and strictness.")
            if returned:
                text += (f"\n\n{returned} of them were already returned to students (email or LMS). "
                         "\"Send to all not sent\" skips them: to send the new grades, select them in "
                         "Return and send to the selected ones.")
            box.setText(text)
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            cb = None
            if n_human:
                cb = QCheckBox(f"Keep the {n_human} question{'s' if n_human != 1 else ''} I graded by hand")
                cb.setChecked(True)
                box.setCheckBox(cb)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            keep_human = cb.isChecked() if cb is not None else True
        deleted = sum(session.reset_for_regrade(s, keep_human=keep_human) for s in subs)
        self.log.appendPlainText(f"Regrading {len(subs)} test(s): {deleted} grade(s) cleared"
                                 + (", hand-graded questions kept." if keep_human else "."))
        self.reload()
        self._go_step(STEP_GRADE)
        self._start(Worker(job_grade, [s.id for s in subs]), "grade")

    def _resume(self):
        self._go_step(STEP_GRADE)
        self.grade_all()

    def stop(self):
        """Stop the running LLM job now (the in-flight request is aborted). Finished work is saved:
        identified tests keep their result, untouched ones stay waiting, graded questions are kept."""
        if self.worker is not None:
            self.worker.stop()
            kind = getattr(self.worker, "kind", "")
            if kind == "import":
                self.lbl_progress.setText("Stopping the loading… (nothing is added)")
            else:
                what = "grading" if kind == "grade" else "reading names"
                self.lbl_progress.setText(f"Stopping {what}… (everything finished so far is saved)")
            self._update_enabled()

    # ---------- worker plumbing ----------
    def _start(self, worker, kind):
        worker.kind = kind
        self.worker = worker
        worker.item.connect(self._on_item)
        worker.progress.connect(self._on_progress)
        worker.done.connect(lambda _: self._finished(None))
        worker.failed.connect(self._finished)
        self.btn_stop_job.setToolTip(
            "Stop loading. Nothing from these files is added." if kind == "import" else
            "Stop the LLM now. Everything already finished is saved; the rest continues when you start it again.")
        self.progress.setRange(0, 0)          # busy until the first progress report
        self.progress.setVisible(True)
        self._update_enabled()
        self._update_steps()
        worker.start()

    def _on_item(self, payload):
        kind, sid = payload[0], payload[1]
        if kind == "region":
            i, total = payload[2], payload[3]
            now = time.monotonic()
            self.log.appendPlainText(f"Test {self._num(self._sub(sid)) if self._sub(sid) else '?'}: "
                                     f"question {i} graded in {now - self._tick:.1f} seconds")
            self._tick = now
            self._region_progress = f"question {i}/{total}"
            sub = self._sub(sid)
            self.lbl_progress.setText(f"Grading test {self._num(sub) if sub else '?'} · {self._region_progress}")
            return
        if kind == "log":
            self.log.appendPlainText(sid)          # (kind, message)
            return
        if kind == "started":
            self._tick = time.monotonic()
        if kind == "graded":
            sub = get_submission(sid)
            if sub:
                grades = list_grades(sid)
                who = self._student_name(sub.student_id) or "unassigned"
                if sub.status == SUB_GRADED:
                    review = "  — needs review" if any(g.needs_human_review for g in grades) else ""
                    self.log.appendPlainText(f"Test {self._num(sub)} ({who}): "
                                             f"{sum(g.score for g in grades):g}/{sum(g.max_points for g in grades):g}"
                                             + review)
                elif sub.status == SUB_ERROR:
                    self.log.appendPlainText(f"Test {self._num(sub)} ({who}): ERROR {sub.error}")
        if kind == "identified":
            sub = get_submission(sid)
            if sub and sub.status == SUB_ERROR:
                self.log.appendPlainText(f"Test {self._num(sub)}: {sub.error}")
        self.reload()
        if sid == self.preview_id:
            self._show_preview_page()

    def _on_progress(self, i, total, msg):
        self.progress.setRange(0, total if total > 0 else 0)   # total 0: busy (page count not known yet)
        self.progress.setValue(i)
        if not msg.startswith("Grading test") or not self._region_progress:
            self.lbl_progress.setText(msg)

    def _finished(self, error):
        stopped = self.worker is not None and self.worker.cancelled
        kind = getattr(self.worker, "kind", "")
        self.worker = None
        self._region_progress = ""
        self.progress.setVisible(False)
        if stopped and kind == "import":
            self.lbl_progress.setText("Loading stopped — nothing was added.")
            self.log.appendPlainText("Loading stopped by user; no pages added.")
        elif stopped:
            self.lbl_progress.setText("Stopped — finished work is saved; start again to continue.")
            self.log.appendPlainText("Stopped by user.")
        elif error:
            self.lbl_progress.setText("")
            QMessageBox.critical(self, "Could not load the scans" if kind == "import" else "Error", error)
        elif kind == "import":
            self.lbl_progress.setText("")
        self.reload()

    def shutdown(self, timeout_ms=3000):
        """Called on app close: ask a running job to stop. Anything unfinished is resumed next start."""
        w = self.worker
        if w is None:
            return
        w.stop()
        if not w.wait(timeout_ms):
            w.terminate()
            w.wait()

class LLMGradeView(ScanView):
    """The grade step on its own page: the AI grades every ready test, skipping the questions a
    human graded (Hand Grade tab or Review)."""
    def __init__(self, template_view=None, students_view=None):
        super().__init__(template_view, students_view, steps=(STEP_GRADE,))
