"""Review graded submissions: per-question cards, score overrides, review flags, export."""
import csv
import os
import numpy as np
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QLabel,
                             QMessageBox, QComboBox, QLineEdit, QSplitter, QScrollArea, QDoubleSpinBox,
                             QHeaderView, QAbstractItemView, QFileDialog, QFrame, QPlainTextEdit, QDialog,
                             QProgressDialog, QApplication, QPushButton)
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QColor, QDesktopServices, QFontInfo, QKeySequence, QShortcut
from ..db import (list_submissions, list_grades, list_students, list_templates, update_submission, current_course,
                  save_grade, delete_submission, get_submission, load_template)
from ..grading.grader import Grader
from ..llm import LLMClient
from ..config import llm_enabled
from ..template.alignment import read_image
from ..models import SUB_GRADED
from . import theme
from .theme import button, card, label, pill, setup_page
from .widgets import ImageThumb, PageViewer, Overlay, Worker, FlowLayout
from .mathtext import latex_html
from ..export import feedback as fb_export

def _needs_review(sub, grades):
    return any(g.needs_human_review for g in grades) or (sub.has_work_on_back and not sub.back_reviewed)

def _is_partial(sub):
    return sub.status != SUB_GRADED

def _region_page(template, grade) -> int:
    if template is None or grade.region_id is None or not (0 <= grade.region_id < len(template.regions)):
        return 0
    return template.regions[grade.region_id].page

_BACK_CODES = ("work_continues_elsewhere", "work_on_back")

def describe_reasons(review_reason: str) -> list:
    """Turn stored reason codes ("low_confidence (0.55); work_on_back") into sentences."""
    out = []
    for raw in (review_reason or "").split(";"):
        r = raw.strip()
        if not r:
            continue
        code = r.split("(")[0].strip()
        arg = r[len(code):].strip(" ()")
        if code == "work_continues_elsewhere":
            out.append("Student says the work continues elsewhere (e.g. on the back) — check the back page")
        elif code == "work_on_back":
            out.append("Writing detected on the back page")
        elif code == "low_confidence":
            try:
                out.append(f"AI unsure (confidence {float(arg) * 100:.0f}%)")
            except ValueError:
                out.append("AI unsure")
        elif code == "poor_alignment":
            out.append("Scan alignment was poor — check the crop")
        elif code == "missing_page":
            out.append("This page is missing from the scan")
        elif code in ("llm_error", "unparsed_response"):
            out.append("AI could not grade this")
        else:
            out.append(r)
    return out or ["Flagged by the AI"]

def involves_back(review_reason: str) -> bool:
    return any(c in (review_reason or "") for c in _BACK_CODES)

def _pending_count(grades) -> int:
    return sum(1 for g in grades if g.needs_human_review)

# keyboard shortcuts of the Review page (window-wide while it is shown; also in the hint line)
KEYS_NEXT_QUESTION, KEYS_PREV_QUESTION = ("Alt+Down",), ("Alt+Up",)
KEYS_NEXT_TEST, KEYS_PREV_TEST = ("Alt+Right",), ("Alt+Left",)
KEYS_ACCEPT_NEXT = ("Ctrl+Return", "Ctrl+Enter")
KEYS_NEXT_FLAGGED = ("F3",)
KEYS_HINT = ("Keys: Alt+↑ / Alt+↓ question  ·  Alt+← / Alt+→ test  ·  Ctrl+Enter accept & next flagged  ·  "
             "F3 next flagged")

def chip_state(g):
    """(mark, color, words) of a question's chip: needs a human, graded by a person, or an AI grade."""
    if g.needs_human_review:
        return "⚠ ", theme.WARN, "needs your grade"
    if g.human_graded:
        return "✓ ", theme.OK, ("graded in Hand Grade" if g.source == "hand" else "graded by you")
    return "", theme.TEXT, "AI grade"

def _style_chip(b: QPushButton, color: str):
    c = QColor(color)
    b.setStyleSheet(
        f"QPushButton {{ background: rgba({c.red()},{c.green()},{c.blue()},22); color: {color}; "
        f"border: 1px solid rgba({c.red()},{c.green()},{c.blue()},70); border-radius: 11px; "
        f"padding: 2px 10px; font-size: 9pt; font-weight: 600; }}"
        f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
        f"QPushButton:checked {{ border: 2px solid {theme.ACCENT}; padding: 1px 9px; }}")

class GradeCard(QFrame):
    """One question of a submission. A flagged question is graded by a human: the AI score is
    only a suggestion until the professor accepts it or sets a score."""
    def __init__(self, grade, on_change, page: int = 0, backs=None, open_path=None,
                 front: str = "", rect=None, view_page=None, rubric: str = "", on_retry=None):
        """front: the (aligned) scan of the question's page; rect: its answer box on that page;
        view_page(path, title, rect): shows a whole page (falls back to open_path);
        rubric: the question's fixed rubric (LaTeX rendered; open while it needs a human);
        on_retry(card): grade this question again with the LLM (None: not possible)."""
        super().__init__()
        self.setObjectName("card")
        self.g, self.on_change = grade, on_change
        self._loading = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label(f"{grade.problem}", "sectionTitle"))
        if page > 0:
            head.addWidget(pill(f"p{page + 1}", theme.ACCENT))
        head.addWidget(label(grade.group, "muted"))
        self.conf_pill = pill("", theme.MUTED)
        head.addWidget(self.conf_pill)
        head.addStretch()
        self.spin = QDoubleSpinBox()
        self.spin.setRange(0, max(grade.max_points, 0)); self.spin.setDecimals(1); self.spin.setSingleStep(0.5)
        self.spin.setValue(grade.score)
        self.spin.setFixedWidth(80)
        head.addWidget(self.spin)
        head.addWidget(label(f"/ {grade.max_points:g}", "muted"))
        lay.addLayout(head)

        self.banner = QFrame(); self.banner.setObjectName("banner")
        bv = QVBoxLayout(self.banner); bv.setContentsMargins(10, 8, 10, 8); bv.setSpacing(4)
        t = QLabel("<b>⚠  Needs human grade</b>")
        bv.addWidget(t)
        self.reason_labels = []
        for text in describe_reasons(grade.review_reason):
            rl = QLabel("•  " + text); rl.setWordWrap(True)
            self.reason_labels.append(rl)
            bv.addWidget(rl)
        brow = QHBoxLayout()
        self.lbl_suggestion = QLabel(f"AI suggestion: <b>{grade.score:g} / {grade.max_points:g}</b>")
        brow.addWidget(self.lbl_suggestion)
        brow.addStretch()
        # back of the question's own sheet; a flagged question falls back to any scanned back
        backs = backs or []
        back = backs[page] if 0 <= page < len(backs) and backs[page] else ""
        if not back and involves_back(grade.review_reason):
            back = next((b for b in backs if b), "")
            if not back:
                brow.addWidget(label("No back page was scanned — check the paper", "muted"))
        self.btn_ok = button("Accept suggestion", "primary",
                             "Keep the AI score as your grade (or just type a different score). "
                             "Ctrl+Enter: accept and go to the next flagged question")
        brow.addWidget(self.btn_ok)
        bv.addLayout(brow)
        lay.addWidget(self.banner)

        thumb = ImageThumb(max_height=240)
        img = read_image(grade.extracted_image_path) if grade.extracted_image_path else None
        thumb.set_image(img)
        lay.addWidget(thumb)

        def show(path, title, box=None):
            if view_page:
                view_page(path, title, box)
            elif open_path:
                open_path(path)
        vrow = QHBoxLayout(); vrow.setSpacing(6)
        self.btn_front = self.btn_back = None
        if front:
            self.btn_front = button("Full page", "ghost", "The whole scanned page, with this answer box outlined")
            self.btn_front.clicked.connect(
                lambda: show(front, f"{grade.problem} — page {page + 1}", rect))
            vrow.addWidget(self.btn_front)
        if back:
            self.btn_back = button("Back of page", "ghost", "The back of this sheet, as scanned")
            self.btn_back.clicked.connect(lambda: show(back, f"{grade.problem} — back of page {page + 1}"))
            vrow.addWidget(self.btn_back)
        self.btn_rubric = button("Rubric", "ghost", "Show or hide the rubric of this question")
        self.btn_rubric.setCheckable(True)
        vrow.addWidget(self.btn_rubric)
        vrow.addStretch()
        self.btn_retry = button("Retry AI grade", "ghost",
                                "Ask the AI to grade this question again; the new score and feedback "
                                "replace the current ones")
        self.btn_retry.setEnabled(on_retry is not None)
        self.btn_retry.setVisible(llm_enabled())        # Settings → "Use an LLM"
        if on_retry is not None:
            self.btn_retry.clicked.connect(lambda: on_retry(self))
        vrow.addWidget(self.btn_retry)
        lay.addLayout(vrow)

        # the rubric the AI graded with: open on questions that need a human, else on demand
        self.rubric_box = QFrame(); self.rubric_box.setObjectName("rubricBox")
        self.rubric_box.setStyleSheet(f"QFrame#rubricBox {{ background: {theme.SURFACE_ALT}; "
                                      f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}")
        rl = QVBoxLayout(self.rubric_box); rl.setContentsMargins(10, 6, 10, 6); rl.setSpacing(2)
        rl.addWidget(QLabel("<b>Rubric</b>"))
        self.lbl_rubric = QLabel()
        self.lbl_rubric.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_rubric.setText(latex_html(rubric.strip(), theme.TEXT, QFontInfo(self.lbl_rubric.font()).pixelSize())
                                if rubric.strip() else "No rubric was set for this question.")
        self.lbl_rubric.setWordWrap(True)
        self.lbl_rubric.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rl.addWidget(self.lbl_rubric)
        lay.addWidget(self.rubric_box)
        self.btn_rubric.toggled.connect(self._show_rubric)
        self.btn_rubric.setChecked(grade.needs_human_review)
        self._show_rubric(grade.needs_human_review)
        if grade.transcription:
            t = QLabel(f"<span style='color:{theme.MUTED}'>Read as:</span> {self._esc(grade.transcription)}")
            t.setWordWrap(True); t.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            t.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
            lay.addWidget(t)
        if grade.next_part_notes:
            t = QLabel(f"<span style='color:{theme.MUTED}'>Note for later parts:</span> "
                       f"{self._esc(grade.next_part_notes)}")
            t.setWordWrap(True); t.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(t)
        lay.addWidget(label("Feedback (on the first page of the returned test)", "muted"))
        self.fb_edit = QPlainTextEdit(grade.feedback)
        self.fb_edit.setMaximumHeight(90)
        lay.addWidget(self.fb_edit)
        self._fb_timer = QTimer(self)
        self._fb_timer.setSingleShot(True)
        self._fb_timer.setInterval(600)
        self._fb_timer.timeout.connect(self.flush)

        self.spin.valueChanged.connect(self._score_changed)
        self.btn_ok.clicked.connect(self._mark_reviewed)
        self.fb_edit.textChanged.connect(self._fb_timer.start)
        self._refresh_state()
        self._loading = False

    def set_current(self, on: bool):
        """Outline the question the keyboard works on (Alt+↑/↓, Ctrl+Enter)."""
        if bool(self.property("current")) != on:
            self.setProperty("current", on)
            self.style().unpolish(self); self.style().polish(self)

    def _show_rubric(self, shown: bool):
        self.rubric_box.setVisible(shown)
        self.btn_rubric.setText("Hide rubric" if shown else "Show rubric")

    def set_retrying(self, busy: bool):
        self.btn_retry.setEnabled(not busy)
        self.btn_retry.setText("Grading…" if busy else "Retry AI grade")

    def _refresh_state(self):
        g = self.g
        pending = g.needs_human_review
        self.banner.setVisible(pending)
        if g.human_graded:
            text, color = ("Hand ✓" if g.source == "hand" else "Human ✓"), theme.OK
        else:
            text = f"AI {g.confidence * 100:.0f}%"
            color = theme.WARN if pending else theme.MUTED
        c = QColor(color)
        self.conf_pill.setText(text)
        self.conf_pill.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},30); color: {color};")
        self.conf_pill.setToolTip("Score set or confirmed by you" if g.human_graded
                                  else "The AI's own confidence in this score")
        self.spin.setStyleSheet(f"QDoubleSpinBox {{ border: 2px solid {theme.WARN}; background: {theme.WARN_SOFT}; }}"
                                if pending else "")
        self.spin.setToolTip("AI suggestion — change it or press Accept to make it your grade" if pending else "")

    def _resolve(self):
        """The professor graded this question: it is no longer an AI suggestion."""
        self.g.human_graded = True
        self.g.needs_human_review = False
        save_grade(self.g)
        self._refresh_state()
        self.on_change()

    def flush(self):
        """Persist edited feedback (debounced while typing; also called before exports)."""
        self._fb_timer.stop()
        text = self.fb_edit.toPlainText()
        if text != self.g.feedback:
            self.g.feedback = text
            save_grade(self.g)

    @staticmethod
    def _esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    def _score_changed(self, v):
        if self._loading:
            return
        self.g.score = float(v)
        self._resolve()   # any score the professor types is a human grade

    def _mark_reviewed(self):
        self._resolve()

class ReviewView(QWidget):
    def __init__(self):
        super().__init__()
        body, actions = setup_page(self, "Review", "Check flagged answers, adjust scores and export results.")
        self.btn_next = button("Next flagged", tooltip="Scroll to the next question that needs a human grade; "
                                                       "after the last one, go to the next test that needs review "
                                                       "(F3)")
        self.btn_preview = button("Preview feedback", tooltip="Show the feedback page of this test's returned PDF")
        self.btn_pdf = button("Feedback PDF…", tooltip="Annotated PDF (red scores + feedback) for this student")
        self.btn_pdf_all = button("Export all PDFs…", "primary",
                                  tooltip="One annotated PDF per student for the selected exam, plus index.csv "
                                          "with names, IDs, emails and file names")
        self.btn_export = button("Export CSV…")
        self.btn_delete = button("Delete submission", "danger")
        for b in (self.btn_next, self.btn_preview, self.btn_pdf, self.btn_pdf_all, self.btn_export, self.btn_delete):
            actions.addWidget(b)
        self.export_worker = None
        self.retry_worker = None

        bar_card, bar = card(QHBoxLayout, margins=10)
        bar.addWidget(label("Exam"))
        self.cb_exam = QComboBox(); self.cb_exam.setMinimumWidth(200)
        bar.addWidget(self.cb_exam)
        bar.addSpacing(10)
        bar.addWidget(label("Show"))
        self.cb_filter = QComboBox(); self.cb_filter.addItems(["All", "Needs human grade", "Done"])
        bar.addWidget(self.cb_filter)
        bar.addSpacing(10)
        self.in_search = QLineEdit(); self.in_search.setPlaceholderText("Search student…")
        self.in_search.setClearButtonEnabled(True)
        bar.addWidget(self.in_search, 1)
        self.lbl_summary = label("", "muted")
        bar.addWidget(self.lbl_summary)
        body.addWidget(bar_card)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        tcard, tl = card(margins=10)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Student", "Version", "Score", "Status"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        tl.addWidget(self.table)
        split.addWidget(tcard)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail = QWidget()
        self.detail.setObjectName("page")
        self.detail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(0, 0, 6, 0)
        self.detail_layout.setSpacing(10)
        self.detail_scroll.setWidget(self.detail)
        # question chips stay above the scrolling cards (always visible while moving between them)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(8)
        self.chip_bar, cbl = card(margins=10)
        cbl.setSpacing(6)
        self.chip_flow = FlowLayout(spacing=6)
        cbl.addLayout(self.chip_flow)
        keys = label(KEYS_HINT, "muted")
        keys.setWordWrap(True)
        keys.setStyleSheet("font-size: 8pt;")
        cbl.addWidget(keys)
        self.chip_bar.setVisible(False)
        rlay.addWidget(self.chip_bar)
        rlay.addWidget(self.detail_scroll, 1)
        split.addWidget(right)
        split.setSizes([420, 700])
        body.addWidget(split, 1)

        self.cb_exam.currentIndexChanged.connect(self.load_submissions)
        self._all_exams_chosen = False       # the user picked "All exams": other tabs don't change it
        self.cb_exam.activated.connect(
            lambda _: setattr(self, "_all_exams_chosen", self.cb_exam.currentData() is None))
        self.cb_filter.currentIndexChanged.connect(self.load_submissions)
        self.in_search.textChanged.connect(self.load_submissions)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.btn_next.clicked.connect(self.next_flagged)
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_preview.clicked.connect(self.preview_feedback)
        self.btn_pdf.clicked.connect(self.export_pdf)
        self.btn_pdf_all.clicked.connect(self.export_all_pdfs)
        self.btn_delete.clicked.connect(self.delete_selected)
        self._rows = []   # (submission, grades) per table row
        self._current = None
        self.grade_cards = []
        self.chips = []          # one per question card, same order
        self._cur_card = None    # the question the keyboard works on (outlined, its chip checked)
        # window-wide while Review is shown (a hidden page's shortcuts are off), so they work
        # right after opening the tab, whatever has the focus
        for keys, slot in ((KEYS_NEXT_QUESTION, self.next_question), (KEYS_PREV_QUESTION, self.prev_question),
                           (KEYS_NEXT_TEST, self.next_test), (KEYS_PREV_TEST, self.prev_test),
                           (KEYS_ACCEPT_NEXT, self.accept_and_next), (KEYS_NEXT_FLAGGED, self.next_flagged)):
            for k in keys:
                QShortcut(QKeySequence(k), self, activated=lambda s=slot: s(),
                          context=Qt.ShortcutContext.WindowShortcut)
        QApplication.instance().focusChanged.connect(self._focus_moved)
        self.refresh()

    # ---------- list ----------
    def context(self):
        """(exam, version id) for the other tabs: the open test's exam and version, else the
        exam picked here ("All exams" gives nothing)."""
        t = self.templates.get(self._current.template_id) if self._current is not None else None
        if t is not None:
            return t.exam_name, t.id
        return self.cb_exam.currentData(), None

    def set_context(self, exam, tid=None):
        """Show this exam at the next refresh, unless "All exams" was picked here (it includes
        every exam)."""
        if exam and not self._all_exams_chosen:
            self._want_exam = exam

    def refresh(self):
        self.templates = {t.id: t for t in list_templates()}          # the current course's exams
        self.students = list_students()                                 # its roster (student picker)
        self.all_students = {s.id: s for s in list_students(all_courses=True)}   # names of any test
        self.student_names = {s.id: s.name for s in self.all_students.values()}
        want, self._want_exam = getattr(self, "_want_exam", None), None
        cur = want if want and any(t.exam_name == want for t in self.templates.values()) \
            else self.cb_exam.currentData()
        self.cb_exam.blockSignals(True)
        self.cb_exam.clear()
        self.cb_exam.addItem("All exams", None)
        for exam in sorted({t.exam_name for t in self.templates.values()}):
            self.cb_exam.addItem(exam, exam)
        self.cb_exam.setCurrentIndex(max(0, self.cb_exam.findData(cur)))
        self.cb_exam.blockSignals(False)
        self.load_submissions()

    def load_submissions(self):
        exam = self.cb_exam.currentData()
        ids = [t.id for t in self.templates.values() if t.exam_name == exam] if exam else list(self.templates)
        subs = list_submissions(template_ids=ids)          # "All exams": every exam of the course
        mode = self.cb_filter.currentText()
        q = self.in_search.text().strip().lower()
        keep_id = self._current.id if self._current else None
        self._rows = []
        self._loaded = []   # every listed test, before the Show/search filters (class average)
        n_review = 0
        shown = 0
        for sub in subs:
            grades = list_grades(sub.id)
            # tests without grades (new / identified / ready) belong to Scan & Names / the grade tabs
            if sub.status != SUB_GRADED and not grades:
                continue
            shown += 1
            self._loaded.append((sub, grades))
            needs = _needs_review(sub, grades)
            n_review += needs
            if (mode == "Needs human grade" and not needs) or (mode == "Done" and needs):
                continue
            name = self.student_names.get(sub.student_id, "")
            if q and q not in name.lower() and q not in (sub.name_read or "").lower():
                continue
            self._rows.append((sub, grades))
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        sel_row = -1
        for sub, grades in self._rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._fill_row(r, sub, grades)
            if sub.id == keep_id:
                sel_row = r
        self.table.blockSignals(False)
        self.lbl_summary.setText(f"{shown} submissions · {n_review} need a human")
        if sel_row >= 0:
            self.table.selectRow(sel_row)
        else:
            self._current = None
            self._clear_detail(placeholder=True)

    def _fill_row(self, r, sub, grades):
        name = self.student_names.get(sub.student_id)
        if name:
            it = QTableWidgetItem(name)
        else:
            it = QTableWidgetItem(f"Unassigned — “{sub.name_read}”" if sub.name_read else "Unassigned")
            it.setForeground(QColor(theme.WARN))
        self.table.setItem(r, 0, it)
        t = self.templates.get(sub.template_id)
        self.table.setItem(r, 1, QTableWidgetItem(f"{t.exam_name} v{t.version_label}" if t else "?"))
        self.table.setItem(r, 2, self._total_item(grades))
        needs = _needs_review(sub, grades)
        n = _pending_count(grades)
        if _is_partial(sub):
            st = QTableWidgetItem("Partially graded")
            st.setForeground(QColor(theme.ACCENT))
            st.setToolTip("Not every question is graded yet: finish it in Hand Grade or LLM Grade")
        else:
            if n:
                text = f"Needs human grade ({n})"
            elif needs:
                text = "Check back page"
            else:
                text = "Done"
            st = QTableWidgetItem(text)
            st.setForeground(QColor(theme.WARN if needs else theme.OK))
        f = st.font(); f.setBold(True); st.setFont(f)
        self.table.setItem(r, 3, st)

    @staticmethod
    def _total_text(grades) -> str:
        txt = f"{sum(g.score for g in grades):g} / {sum(g.max_points for g in grades):g}"
        return txt + " *" if _pending_count(grades) else txt

    def _total_item(self, grades) -> QTableWidgetItem:
        it = QTableWidgetItem(self._total_text(grades))
        if _pending_count(grades):
            it.setToolTip("Provisional: includes AI suggestions awaiting human grade")
            it.setForeground(QColor(theme.WARN))
        return it

    # ---------- detail ----------
    def _flush_cards(self):
        for gc in getattr(self, "grade_cards", []):
            try:
                gc.flush()
            except RuntimeError:   # widget already deleted
                pass

    def _clear_detail(self, placeholder=False):
        self._flush_cards()
        self.grade_cards = []
        self.chips = []
        self._cur_card = None
        while self.chip_flow.count():
            w = self.chip_flow.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.chip_bar.setVisible(False)
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if placeholder:
            ph = label("Select a submission to review it.", "muted")
            ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.detail_layout.addWidget(ph, 1)

    def _selected_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows[0] if rows else -1

    def show_detail(self):
        row = self._selected_row()
        if row < 0:
            return
        sub, grades = self._rows[row]
        self._current = sub
        self._clear_detail()

        head, hl = card()
        top = QHBoxLayout()
        cb = QComboBox()
        cb.addItem("— unassigned —", None)
        for s in self.students:
            cb.addItem(s.name + (f"  ({s.student_id})" if s.student_id else ""), s.id)
        cb.setCurrentIndex(max(0, cb.findData(sub.student_id)))
        cb.currentIndexChanged.connect(lambda _: self._reassign(sub, cb.currentData()))
        cb.setMinimumWidth(240)
        top.addWidget(label("Student"))
        top.addWidget(cb)
        if sub.name_read:
            top.addWidget(label(f"read as “{sub.name_read}”", "muted"))
        top.addStretch()
        self.lbl_avg = label("", "muted")
        top.addWidget(self.lbl_avg)
        top.addSpacing(12)
        self.lbl_total = label("", "pageTitle")
        top.addWidget(self.lbl_total)
        hl.addLayout(top)

        info = QHBoxLayout()
        t = self.templates.get(sub.template_id)
        info.addWidget(pill(f"{t.exam_name} · v{t.version_label}" if t else "template missing", theme.ACCENT))
        groups = {}
        for g in grades:
            s, m = groups.get(g.group, (0.0, 0.0))
            groups[g.group] = (s + g.score, m + g.max_points)
        self.group_pills = []
        for grp, (s, m) in groups.items():
            p = pill(f"{grp}: {s:g}/{m:g}", theme.MUTED)
            self.group_pills.append((grp, p))
            info.addWidget(p)
        info.addStretch()
        hl.addLayout(info)

        pages_row = QHBoxLayout()
        self.page_buttons = []
        aligned = sub.aligned_pages or ([sub.aligned_front_path] if sub.aligned_front_path else [])
        for p, scan in enumerate(sub.pages):
            path = aligned[p] if p < len(aligned) and aligned[p] else scan
            b = button(f"Page {p + 1}", "ghost", path)
            b.clicked.connect(lambda _, x=path: self._open(x))
            pages_row.addWidget(b)
            self.page_buttons.append(b)
        for p, back in enumerate(sub.backs):
            if back:
                b = button(f"Back {p + 1}", "ghost", back)
                b.clicked.connect(lambda _, x=back: self._open(x))
                pages_row.addWidget(b)
                self.page_buttons.append(b)
        pages_row.addStretch()
        if _is_partial(sub):
            pages_row.addWidget(label("Partially graded — finish it in Hand Grade or LLM Grade", "muted"))
        hl.addLayout(pages_row)

        # one chip per question: its state and score at a glance; a click jumps to it
        for i, g in enumerate(grades):
            b = QPushButton()
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, i=i: self._chip_clicked(i))
            self.chip_flow.addWidget(b)
            self.chips.append(b)
        self.chip_bar.setVisible(bool(grades))

        if sub.has_work_on_back and not sub.back_reviewed:
            ban = QFrame(); ban.setObjectName("banner")
            bl = QHBoxLayout(ban); bl.setContentsMargins(10, 6, 10, 6)
            bl.addWidget(QLabel("⚠  Work detected on the back page. Check it and adjust scores."), 1)
            b = button("Mark back reviewed")
            b.clicked.connect(lambda: self._mark_back(sub))
            bl.addWidget(b)
            hl.addWidget(ban)
        self.detail_layout.addWidget(head)

        self.grade_cards = []
        for g in grades:
            p = _region_page(t, g)
            front = (aligned[p] if p < len(aligned) and aligned[p] else
                     sub.pages[p] if p < len(sub.pages) else "")
            region = (t.regions[g.region_id] if t is not None and g.region_id is not None
                      and 0 <= g.region_id < len(t.regions) else None)
            gc = GradeCard(g, lambda: self._grades_changed(row), p, backs=sub.backs, open_path=self._open,
                           front=front, rect=region.rect if region else None, view_page=self.view_page,
                           rubric=region.rubric.as_text(region.points) if region else "",
                           on_retry=self.retry_grade if region is not None else None)
            self.grade_cards.append(gc)
            self.detail_layout.addWidget(gc)
        self.detail_layout.addStretch()
        self._update_total(grades)
        self._update_chips()
        if self.grade_cards:     # outlined; the focus stays where it is (e.g. the table: ↑/↓ pick tests)
            self._set_current(self.grade_cards[0])

    def _update_total(self, grades):
        self.lbl_total.setText(self._total_text(grades))
        self.lbl_total.setToolTip("Provisional: includes AI suggestions awaiting human grade"
                                  if _pending_count(grades) else "")
        groups = {}
        for g in grades:
            s, m = groups.get(g.group, (0.0, 0.0))
            groups[g.group] = (s + g.score, m + g.max_points)
        for grp, p in self.group_pills:
            s, m = groups[grp]
            p.setText(f"{grp}: {s:g}/{m:g}")
        self._update_average()

    def _exam_of(self, sub) -> str:
        t = self.templates.get(sub.template_id)
        return t.exam_name if t else sub.exam_name

    def class_average(self, sub):
        """(average score, average max, number of tests, all tests have the same max) over the
        fully graded tests of `sub`'s exam (every version); None if there are none. Questions
        still awaiting a human grade count with the AI suggestion."""
        exam = self._exam_of(sub)
        totals = [(sum(g.score for g in gr), sum(g.max_points for g in gr))
                  for s, gr in getattr(self, "_loaded", []) if not _is_partial(s) and gr and self._exam_of(s) == exam]
        if not totals:
            return None
        n = len(totals)
        return (sum(s for s, _ in totals) / n, sum(m for _, m in totals) / n, n,
                len({m for _, m in totals}) == 1)

    def _update_average(self):
        avg = self.class_average(self._current) if self._current else None
        if avg is None:
            self.lbl_avg.setText("")
            return
        s, m, n, same_max = avg
        pct = f"{100 * s / m:.0f}%" if m else "—"
        # versions with different totals: only the percentage is comparable
        self.lbl_avg.setText(f"Class average {s:.1f} / {m:g} · {pct}" if same_max else f"Class average {pct}")
        self.lbl_avg.setToolTip(f"Average of the {n} fully graded test{'s' if n != 1 else ''} of "
                                f"{self._exam_of(self._current)} (all versions). Questions still "
                                "awaiting a human grade count with the AI suggestion.")

    def _grades_changed(self, row):
        sub, grades = self._rows[row]
        self._update_total(grades)
        self._fill_row(row, sub, grades)
        self._update_chips()

    def _reassign(self, sub, student_id):
        sub.student_id = student_id
        update_submission(sub)
        row = self._selected_row()
        if row >= 0:
            self._fill_row(row, *self._rows[row])

    def _mark_back(self, sub):
        sub.back_reviewed = True
        update_submission(sub)
        # back-page flags on individual grades are resolved too
        for g in self._rows[self._selected_row()][1]:
            reasons = [r.strip() for r in g.review_reason.split(";") if r.strip()]
            if g.needs_human_review and "work_on_back" in reasons:
                reasons.remove("work_on_back")
                g.review_reason = "; ".join(reasons)
                g.needs_human_review = bool(reasons)
                save_grade(g)
        self.load_submissions()

    def next_flagged(self, after=None):
        """Scroll to the next question of this test (below the current view, or after the card
        `after`) that needs a human grade; when none is left, select the next test that needs
        review (after the current one, wrapping) and scroll to its first such question. The
        question's score box gets the focus. Returns the selected row or -1."""
        start = self._selected_row()
        if start >= 0 and self._scroll_to_pending(below_view=True, after=after):
            return start
        n = len(self._rows)
        for k in range(1, n + 1):
            r = (start + k) % n if start >= 0 else k - 1
            sub, grades = self._rows[r]
            if _needs_review(sub, grades):
                if r != start:
                    self.table.selectRow(r)   # rebuilds the detail; scroll once it is laid out
                    self.table.scrollToItem(self.table.item(r, 0))
                    self.detail_scroll.verticalScrollBar().setValue(0)
                    QTimer.singleShot(0, lambda: self._scroll_to_pending(below_view=False))
                else:
                    self._scroll_to_pending(below_view=False)
                return r
        self.window().statusBar().showMessage("No submissions need review.", 3000) \
            if hasattr(self.window(), "statusBar") else None
        return -1

    def _scroll_to_pending(self, below_view: bool, after=None) -> bool:
        """Put the next question card that needs a human grade at the top of the detail.
        after: only cards after this one; below_view: only cards after the one last jumped to
        (or below the scrolled-to point); otherwise the first one of the test. False when
        there is none."""
        try:
            pending = [(i, gc) for i, gc in enumerate(self.grade_cards) if gc.g.needs_human_review]
        except RuntimeError:   # cards already deleted
            return False
        sb = self.detail_scroll.verticalScrollBar()
        if after is not None and after in self.grade_cards:
            idx = self.grade_cards.index(after)
            pending = [(i, gc) for i, gc in pending if i > idx]
        elif below_view:
            jump = getattr(self, "_jump", None)
            if jump and jump[0] in self.grade_cards and sb.value() == jump[1]:
                # the last card jumped to may not reach the top (end of the page): go past it
                after = self.grade_cards.index(jump[0])
                pending = [(i, gc) for i, gc in pending if i > after]
            else:
                pending = [(i, gc) for i, gc in pending if gc.y() > sb.value() + 4]
        if not pending:
            return False
        self._focus_card(pending[0][1])
        return True

    # ---------- keyboard: questions and tests ----------
    def _cards(self) -> list:
        """The question cards of the open test (none once they are deleted)."""
        try:
            for gc in self.grade_cards:
                gc.objectName()                 # raises once the widget is deleted
        except RuntimeError:
            return []
        return list(self.grade_cards)

    def _card_of(self, w):
        """The question card holding widget `w`, or None."""
        while w is not None:
            if isinstance(w, GradeCard):
                return w if w in self.grade_cards else None
            w = w.parentWidget()
        return None

    def _set_current(self, gc):
        """Outline `gc` and check its chip (no scrolling, the focus stays)."""
        self._cur_card = gc
        for c in self.grade_cards:
            try:
                c.set_current(c is gc)
            except RuntimeError:
                pass
        for b, c in zip(self.chips, self.grade_cards):
            b.setChecked(c is gc)

    def _focus_card(self, gc, scroll: bool = True):
        """Make `gc` the current question: scroll it to the top, outline it and put the cursor
        in its score box (type a score, or Ctrl+Enter to accept the suggestion)."""
        if scroll:
            # lay the cards out again first: accepting a question hides its banner, which moves the
            # cards below it up; their old position would scroll past the top of `gc`. Each card
            # first (its new height), then the column of cards.
            for c in self._cards():
                c.layout().activate()
            self.detail_layout.invalidate()
            self.detail_layout.activate()
            sb = self.detail_scroll.verticalScrollBar()
            sb.setValue(gc.y())
            self._jump = (gc, sb.value())
        self._set_current(gc)
        gc.spin.setFocus(Qt.FocusReason.ShortcutFocusReason)
        gc.spin.selectAll()

    def _current_card(self):
        """The question the keys work on: the one holding the focus, else the outlined one if it
        is on screen, else the first one on screen."""
        cards = self._cards()
        if not cards:
            return None
        gc = self._card_of(QApplication.focusWidget())
        if gc is not None:
            return gc
        top = self.detail_scroll.verticalScrollBar().value()
        bottom = top + self.detail_scroll.viewport().height()
        on_screen = [c for c in cards if c.y() < bottom and c.y() + c.height() > top]
        if self._cur_card in on_screen:
            return self._cur_card
        return on_screen[0] if on_screen else cards[0]

    def _focus_moved(self, _old, new):
        """Clicking or tabbing into a question makes it the current one."""
        try:
            if not self.isVisible():
                return
            gc = self._card_of(new)
        except RuntimeError:
            return
        if gc is not None and gc is not self._cur_card:
            self._set_current(gc)

    def _chip_clicked(self, i: int):
        if 0 <= i < len(self.grade_cards):
            self._focus_card(self.grade_cards[i])

    def _update_chips(self):
        for b, gc in zip(self.chips, self.grade_cards):
            g = gc.g
            mark, color, words = chip_state(g)
            b.setText(f"{mark}{g.problem}  {g.score:g}/{g.max_points:g}")
            b.setToolTip(f"{g.problem}: {g.score:g} / {g.max_points:g} · {words}. Click to go to it.")
            _style_chip(b, color)

    def _step_question(self, step: int) -> bool:
        cards = self._cards()
        cur = self._current_card()
        if cur is None:
            return False
        i = cards.index(cur) + step
        if not 0 <= i < len(cards):
            self._status("This is the last question of the test." if step > 0 else
                         "This is the first question of the test.")
            return False
        self._focus_card(cards[i])
        return True

    def next_question(self):
        """Alt+↓: the next question of this test."""
        return self._step_question(1)

    def prev_question(self):
        """Alt+↑: the previous question of this test."""
        return self._step_question(-1)

    def _step_test(self, step: int) -> int:
        """Open the next/previous test of the list at its first question. Returns its row or -1."""
        n = self.table.rowCount()
        if not n:
            return -1
        row = self._selected_row()
        target = (0 if step > 0 else n - 1) if row < 0 else row + step
        if not 0 <= target < n:
            self._status("This is the last test in the list." if step > 0 else "This is the first test in the list.")
            return -1
        self.table.selectRow(target)          # rebuilds the question cards
        self.table.scrollToItem(self.table.item(target, 0))
        self.detail_scroll.verticalScrollBar().setValue(0)
        if self.grade_cards:
            self._focus_card(self.grade_cards[0], scroll=False)
        return target

    def next_test(self):
        """Alt+→: the next test in the list."""
        return self._step_test(1)

    def prev_test(self):
        """Alt+←: the previous test in the list."""
        return self._step_test(-1)

    def accept_and_next(self):
        """Ctrl+Enter: the current question's score becomes your grade (a flagged question's AI
        suggestion is accepted; a score you typed already counts), then on to the next flagged
        question. Questions nobody flagged are left as they are."""
        gc = self._current_card()
        if gc is None:
            return -1
        gc.spin.interpretText()               # a score still being typed
        gc.flush()                            # feedback being typed
        if gc.g.needs_human_review:
            gc._mark_reviewed()
        return self.next_flagged(after=gc)

    def retry_grade(self, card):
        """Grade one question again with the LLM, in the background. The new grade replaces the
        saved one (after a confirmation if a human graded it); on an LLM error nothing changes."""
        if self.retry_worker is not None:
            self._status("Already re-grading a question — wait for it to finish.")
            return
        g = card.g
        if g.human_graded and QMessageBox.question(
                self, "Retry AI grade",
                f"You already graded {g.problem} ({g.score:g} / {g.max_points:g}). Replace your grade "
                "and feedback with a new AI grade?") != QMessageBox.StandardButton.Yes:
            return
        card.flush()
        sid, region_id = g.submission_id, g.region_id

        def job(worker=None):
            sub = get_submission(sid)
            template = load_template(sub.template_id) if sub else None
            if template is None:
                raise RuntimeError("The test or its template no longer exists.")
            grader = Grader(LLMClient.for_grading())
            worker.llm = grader.llm
            return grader.regrade_region(template, sub, region_id)

        def finish(msg):
            self.retry_worker = None
            try:
                card.set_retrying(False)
            except RuntimeError:   # the card was rebuilt meanwhile
                pass
            if msg:
                self._status(msg)

        def on_done(new):
            finish(f"{new.problem} re-graded by the AI: {new.score:g} / {new.max_points:g}.")
            self._reload_keep_scroll()

        def on_fail(err):
            finish("")
            QMessageBox.warning(self, "Retry AI grade",
                                f"The AI could not grade {g.problem}; the current grade was kept.\n\n{err}")

        card.set_retrying(True)
        self.retry_worker = Worker(job)
        self.retry_worker.done.connect(on_done)
        self.retry_worker.failed.connect(on_fail)
        self.retry_worker.start()

    def _reload_keep_scroll(self):
        sb = self.detail_scroll.verticalScrollBar()
        pos = sb.value()
        self.load_submissions()
        QTimer.singleShot(0, lambda: sb.setValue(pos))

    def _status(self, msg):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(msg, 5000)

    def _open(self, path):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def view_page(self, path: str, title: str, rect=None):
        """Show a whole scanned page in a window that stays open while grading (one window,
        reused). rect: the question's answer box, outlined and scrolled into view."""
        img = read_image(path) if path else None
        if img is None:
            QMessageBox.information(self, "Page", "This page image is missing.")
            return
        dlg = getattr(self, "_page_dlg", None)
        if dlg is None:
            dlg = self._page_dlg = QDialog(self)
            dlg.resize(900, 1000)
            lay = QVBoxLayout(dlg)
            dlg.viewer = PageViewer()
            dlg.viewer.canvas.draw_enabled = False
            lay.addWidget(dlg.viewer, 1)
            row = QHBoxLayout()
            row.addWidget(label("Ctrl+wheel to zoom", "muted")); row.addStretch()
            dlg.btn_external = button("Open in image viewer", "ghost")
            dlg.btn_external.clicked.connect(lambda: self._open(dlg.path))
            row.addWidget(dlg.btn_external)
            lay.addLayout(row)
        dlg.path = path
        dlg.setWindowTitle(title)
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        dlg.viewer.set_image(img)
        dlg.viewer.canvas.set_overlays([Overlay(tuple(rect), QColor(theme.ACCENT), "")] if rect else [])
        if rect:
            s = dlg.viewer.canvas.scale()
            x, y, w, h = rect
            dlg.viewer.ensureVisible(int((x + w / 2) * s), int((y + h / 2) * s),
                                     int(w * s / 2) + 40, int(h * s / 2) + 80)

    def delete_selected(self):
        row = self._selected_row()
        if row < 0:
            return
        sub, _ = self._rows[row]
        who = self.student_names.get(sub.student_id, "this submission")
        if QMessageBox.question(self, "Delete", f"Delete the graded submission for {who}?") != QMessageBox.StandardButton.Yes:
            return
        delete_submission(sub.id)
        self._current = None
        self.load_submissions()

    # ---------- feedback PDFs ----------
    def _current_bundle(self):
        row = self._selected_row()
        if row < 0:
            return None
        self._flush_cards()
        sub, _ = self._rows[row]
        grades = list_grades(sub.id)
        return sub, self.templates.get(sub.template_id), grades, self.all_students.get(sub.student_id)

    def render_preview_page(self):
        """First page (scores + all feedback, PIL image) of the selected submission's returned
        PDF, or None."""
        b = self._current_bundle()
        if b is None:
            return None
        pages = fb_export.render_feedback_pages(*b)
        return pages[0] if pages else None

    def preview_feedback(self):
        img = self.render_preview_page()
        if img is None:
            QMessageBox.information(self, "Preview", "Select a graded submission first."); return
        dlg = QDialog(self)
        dlg.setWindowTitle("Feedback preview — page 1")
        dlg.resize(900, 1000)
        lay = QVBoxLayout(dlg)
        viewer = PageViewer()
        viewer.canvas.draw_enabled = False
        lay.addWidget(viewer)
        rgb = np.asarray(img.convert("RGB"))
        viewer.set_image(rgb[:, :, ::-1].copy())   # PageViewer expects BGR
        dlg.exec()

    def export_pdf(self):
        b = self._current_bundle()
        if b is None:
            QMessageBox.information(self, "Feedback PDF", "Select a graded submission first."); return
        sub, t, _, student = b
        default = fb_export.pdf_filename(sub, student, t.exam_name if t else sub.exam_name)
        path, _ = QFileDialog.getSaveFileName(self, "Save feedback PDF", default, "PDF (*.pdf)")
        if not path:
            return
        try:
            fb_export.export_submission_pdf(sub, path)
        except Exception as e:
            QMessageBox.critical(self, "Feedback PDF", str(e)); return
        self._open(path)

    def export_all_pdfs(self):
        exam = self.cb_exam.currentData()
        if not exam:
            QMessageBox.information(self, "Export", "Choose an exam in the filter first."); return
        if self.export_worker is not None:
            return
        out_dir = QFileDialog.getExistingDirectory(self, f"Folder for the {exam} feedback PDFs")
        if not out_dir:
            return
        self._flush_cards()
        partial = sum(1 for s, _ in self._rows if _is_partial(s))
        if partial and QMessageBox.question(
                self, "Export", f"{partial} test(s) are only partially graded and will be skipped. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        prog = QProgressDialog("Exporting feedback PDFs…", None, 0, 1, self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)

        course = current_course()

        def job(worker=None):
            return fb_export.export_exam(exam, out_dir, course_id=course,
                                         progress=lambda i, n, m: worker.progress.emit(i, n, m))

        def on_progress(i, n, msg):
            prog.setMaximum(max(1, n)); prog.setValue(i); prog.setLabelText(msg)

        def on_done(res):
            self.export_worker = None
            prog.close()
            if QMessageBox.question(self, "Export finished",
                                    f"Exported {len(res)} PDFs and index.csv to\n{out_dir}\n\nOpen the folder?"
                                    ) == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))

        def on_fail(err):
            self.export_worker = None
            prog.close()
            QMessageBox.critical(self, "Export failed", err)

        self.export_worker = Worker(job)
        self.export_worker.progress.connect(on_progress)
        self.export_worker.done.connect(on_done)
        self.export_worker.failed.connect(on_fail)
        self.export_worker.start()

    # ---------- export ----------
    def export_csv(self):
        if not self._rows:
            QMessageBox.information(self, "Export", "Nothing to export."); return
        path, _ = QFileDialog.getSaveFileName(self, "Export grades", "grades.csv", "CSV (*.csv)")
        if not path:
            return
        problems = []
        for _, grades in self._rows:
            for g in grades:
                if g.problem not in problems:
                    problems.append(g.problem)
        sid_by_id = {s.id: s.student_id for s in self.all_students.values()}
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["name", "student_id", "exam", "version", *problems, "total", "max", "needs_review"])
            for sub, grades in self._rows:
                t = self.templates.get(sub.template_id)
                by_p = {g.problem: g.score for g in grades}
                w.writerow([self.student_names.get(sub.student_id, sub.name_read), sid_by_id.get(sub.student_id, ""),
                            t.exam_name if t else "", t.version_label if t else "",
                            *[by_p.get(p, "") for p in problems],
                            sum(g.score for g in grades), sum(g.max_points for g in grades),
                            "yes" if _needs_review(sub, grades) else "no"])
        self.window().statusBar().showMessage(f"Exported {len(self._rows)} rows to {path}", 5000)
