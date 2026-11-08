"""Template editor: exams, versions, pages, answer/question/name regions, LaTeX questions and rubrics.

Workflow (shown as steps in the right panel):
  1. Auto-setup with LLM, in two phases: (a) detect regions + name/ID fields on every page, then
     STOP and let the human check the boxes and points; (b) on "Continue", transcribe each
     question to LaTeX
  2. Human checks/adjusts the boxes and sets the points per question
  3. LLM generates rubrics from the fixed question + points
  4. Human checks/edits the rubrics
Every LLM job has a Stop button (finished items are kept). The scan layout (blank/back pages in
the scanned order) and an optional column count are set per version. Every change is autosaved.
"""
import copy
import inspect
from pathlib import Path
from typing import Dict, List, Optional
from PyQt6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
                             QFileDialog, QMessageBox, QInputDialog, QFormLayout, QLineEdit,
                             QDoubleSpinBox, QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem,
                             QButtonGroup, QProgressBar, QScrollArea, QComboBox, QCheckBox, QFrame,
                             QDialog, QDialogButtonBox, QLabel)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut, QColor

from ..db import (list_templates, load_template, delete_template, list_versions,
                  get_exam_instructions, set_exam_instructions, get_exam_strictness, set_exam_strictness,
                  list_submissions, count_region_grades, count_template_grades, current_course, list_courses,
                  course_name, copy_exam, move_exam, exam_test_count)
from ..template.template import (create_template_from_image, add_region, persist, duplicate_as_version,
                                 part_of, siblings, question_context, repair_part_json)
from ..template.alignment import read_image, crop_region
from ..scanner.image_input import import_pages, IMAGE_FILTER
from ..models import TestTemplate, AnswerRegion, BLANK, double_sided_layout, SUB_GRADED, SUB_GRADING
from ..template.layout import parse_label, _part_key
from .rubric_editor import RubricEditor
from ..llm.client import (LLMClient, image_to_b64, prepare_crop, downscale_for_llm, STRICTNESS_NAMES,
                          STRICTNESS_TEXT)
from ..template.layout import refine_layout, iou
from ..template.answer_key import load_answer_key, apply_answer_key, SKILL_PATH
from ..template.tex_import import load_tex
from ..config import CROP_PADDING, LLM_CONFIG, DEFAULT_POINTS, STRICTNESS_MIN, STRICTNESS_MAX, llm_enabled
from . import theme
from .theme import button, card, label, setup_page
from .widgets import PageViewer, Overlay, Worker

MODE_ANSWER, MODE_QUESTION, MODE_NAME, MODE_ID, MODE_VERSION = 0, 1, 2, 3, 4
AUTOSAVE_MS = 800
COLOR_ID = QColor(14, 116, 144)     # teal: student ID field
COLOR_VERSION = QColor(180, 83, 9)  # amber: where the exam states its version
STOPPED_MSG = "Stopped — finished items were kept."

def layout_text(layout: List[int]) -> str:
    """Scanned page order, e.g. '1 · B · 2 · B'."""
    return " · ".join("B" if p == BLANK else str(p + 1) for p in layout)

class ScanLayoutDialog(QDialog):
    """Edit where blank (not-in-template) pages sit in a scanned test."""
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setWindowTitle("Scanned page order")
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Order of the pages in one scanned test. “Blank” pages (e.g. back sides) are "
                            "not part of the template; work found on them is shown to you for review.", "muted"))
        lay.itemAt(0).widget().setWordWrap(True)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.btn_insert = button("Insert blank after")
        self.btn_remove = button("Remove blank", "danger")
        row.addWidget(self.btn_insert); row.addWidget(self.btn_remove); row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)
        self.btn_insert.clicked.connect(self._insert)
        self.btn_remove.clicked.connect(self._remove)
        self.list.currentRowChanged.connect(lambda _: self._update())
        self.refresh()

    def refresh(self, select=None):
        lay = self.editor.t.layout
        self.list.clear()
        for p in lay:
            self.list.addItem("Blank page" if p == BLANK else f"Template page {p + 1}")
        if select is not None:
            self.list.setCurrentRow(min(select, len(lay) - 1))
        self._update()

    def _update(self):
        row = self.list.currentRow()
        lay = self.editor.t.layout
        self.btn_insert.setEnabled(row >= 0)
        self.btn_remove.setEnabled(0 <= row < len(lay) and lay[row] == BLANK)

    def _insert(self):
        row = self.list.currentRow()
        self.editor.insert_blank(row)
        self.refresh(row + 1)

    def _remove(self):
        row = self.list.currentRow()
        self.editor.remove_blank(row)
        self.refresh(max(0, row - 1))

# ---- background LLM jobs (run in Worker threads, results applied on the UI thread) ----
class _Pages:
    """Lazy, cached page images for worker threads. Accepts page paths or a single page image."""
    def __init__(self, paths):
        if hasattr(paths, "shape"):          # a single already-loaded page
            self.paths, self._imgs = [""], {0: paths}
        else:
            self.paths, self._imgs = list(paths), {}

    def __getitem__(self, i):
        if i not in self._imgs:
            img = read_image(self.paths[i])
            if img is None:
                raise ValueError(f"Cannot read template page {i + 1}: {self.paths[i]}")
            self._imgs[i] = img
        return self._imgs[i]

def _question_crop(pages: _Pages, region):
    img = pages[region.page if 0 <= region.page < len(pages.paths) else 0]
    return prepare_crop(crop_region(img, region.question_rect or region.rect, pad=CROP_PADDING))

def _client(worker) -> LLMClient:
    """Create the LLM client for a job and register it so Stop can abort an in-flight request."""
    llm = LLMClient.from_config()
    worker.llm = llm
    return llm

class _Stopped(Exception):
    pass

def _call(worker, fn, *args, **kw):
    """Run one LLM call. Raises _Stopped if the user pressed Stop (before the call, or the call
    failed because Stop aborted it); real errors propagate when no stop was requested."""
    cancelled = lambda: getattr(worker, "cancelled", False)   # minimal test workers lack it
    if cancelled():
        raise _Stopped()
    try:
        return fn(*args, **kw)
    except Exception:
        if cancelled():
            raise _Stopped()
        raise

def first_parts(t: TestTemplate) -> set:
    """Indices of the regions that are the first part of a question with parts (e.g. 1a)."""
    return {siblings(t, i)[0] for i, r in enumerate(t.regions) if part_of(r.problem)}

def _takes_kw(fn, name) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False

def _extract_one(llm, worker, pages, i, r, first=None):
    """Transcribe region r's question -> (text, stem or '').
    The first part of a question (first=True; default: part (a)) also returns the main
    question's text (stem), emitted as ('stem', '1', text); the editor stores it only if that
    question has no stem yet. Later parts are asked for their own text only."""
    b64 = image_to_b64(_question_crop(pages, r))
    part = part_of(r.problem)
    if first is None:
        first = bool(part) and part[1] in ("a", "i")
    stem = ""
    if part and first and hasattr(llm, "extract_part"):
        d = _call(worker, llm.extract_part, b64, *part)
        stem, text = (d.get("stem") or "").strip(), d.get("part", "")
        if stem:
            worker.item.emit(("stem", part[0], stem))
    elif part and _takes_kw(llm.extract_question, "part_of"):
        text = _call(worker, llm.extract_question, b64, part_of=part)
    else:
        text = _call(worker, llm.extract_question, b64)
    worker.item.emit(("question", i, text))
    return text, stem

def _extract_into(llm, pages, regions, worker, offset=0, total=None, firsts=None):
    total = total or len(regions)
    for n, (i, r) in enumerate(regions):
        worker.progress.emit(offset + n, total, f"Reading question {r.problem} (page {r.page + 1})…")
        _extract_one(llm, worker, pages, i, r, None if firsts is None else i in firsts)

def job_extract(page_paths, regions, firsts=None, worker=None):
    """Question image -> LaTeX. regions: list of (index, region); firsts: indices of first parts
    (see first_parts) — only those are asked for the main question. Stops cleanly on Stop."""
    llm = _client(worker)
    try:
        _extract_into(llm, _Pages(page_paths), regions, worker, firsts=firsts)
    except _Stopped:
        return
    worker.progress.emit(len(regions), len(regions), "Done")

def job_rubric(page_paths, regions, instructions="", template=None, worker=None):
    """Fixed LaTeX question + points -> rubric. Extracts the question first if missing.
    instructions: the exam's grading instructions; each region's grading_notes are used too.
    template: a copy of the template; parts then get the main question + earlier parts as context."""
    llm = _client(worker)
    pages = _Pages(page_paths)
    firsts = first_parts(template) if template is not None else None
    try:
        for n, (i, r) in enumerate(regions):
            question = r.question_text
            if not question.strip():
                worker.progress.emit(n, len(regions), f"Reading question {r.problem}…")
                question, stem = _extract_one(llm, worker, pages, i, r,
                                              None if firsts is None else i in firsts)
                if template is not None:
                    template.regions[i].question_text = question
                    num = (part_of(r.problem) or ("",))[0]
                    if stem and not template.stems.get(num, "").strip():
                        template.stems[num] = stem
            if template is not None:
                question = question_context(template, i)
            worker.progress.emit(n, len(regions), f"Writing rubric for {r.problem}…")
            key = {"answer_key": r.answer_key} if r.answer_key.strip() else {}
            worker.item.emit(("rubric", i, _call(worker, llm.generate_rubric, question, r.points, r.problem,
                                                 instructions=instructions, notes=r.grading_notes, **key)))
    except _Stopped:
        return
    worker.progress.emit(len(regions), len(regions), "Done")

def _detect_one(llm, worker, small, w, h, sw, sh, columns):
    try:
        return _call(worker, llm.detect_layout, image_to_b64(small), w, h, sw, sh, columns=columns)
    except TypeError:   # older detect_layout without the columns hint
        return _call(worker, llm.detect_layout, image_to_b64(small), w, h, sw, sh)

def detect_pages(llm, pages: _Pages, worker, total_steps=None, columns=0):
    """One detect_layout call per page. Returns [(page index, refined layout)] for the pages
    processed — fewer than all pages if the user pressed Stop."""
    out = []
    n = len(pages.paths)
    for p in range(n):
        worker.progress.emit(p, total_steps or n, f"Finding questions on page {p + 1} of {n}…")
        img = pages[p]
        h, w = img.shape[:2]
        small, sw, sh = downscale_for_llm(img)
        try:
            layout = _detect_one(llm, worker, small, w, h, sw, sh, columns)
        except _Stopped:
            break
        out.append((p, refine_layout(img, layout)))
    return out

def _label_key(label: str) -> str:
    return label.strip().rstrip(".").lower().lstrip("q")

def regions_from_layouts(page_layouts, existing: List[AnswerRegion], replace: bool):
    """Merge per-page detections into a region list. Numbering continues across pages.
    In replace mode only the regions on the pages that were detected are replaced (a stopped
    run keeps the regions of pages it never reached).
    Returns (regions, name_rect or None, name_page, number added, id_rect or None, id_page)."""
    done_pages = {p for p, _ in page_layouts}
    if replace:
        regions = [copy.copy(r) for r in existing if r.page not in done_pages]
    else:
        regions = [copy.copy(r) for r in existing]
    prev = existing[-1] if existing else None
    group = prev.group if prev else "default"
    seen = {_label_key(r.problem) for r in regions}
    name_rect, name_page, id_rect, id_page, added = None, 0, None, 0, 0
    for page, layout in page_layouts:
        for q in layout.get("questions", []):
            answer = q.get("answer_rect") or q.get("question_rect")
            if not answer:
                continue
            lab = (q.get("problem") or "").strip().rstrip(".")
            key = _label_key(lab)
            if not replace and ((key and key in seen) or
                                any(r.page == page and iou(answer, r.rect) > 0.3 for r in regions)):
                continue
            problem = (lab if lab.lower().startswith("q") else f"Q{lab}") if lab else f"Q{len(regions) + 1}"
            # every new question starts at DEFAULT_POINTS (printed points are not trusted);
            # the professor sets the real value per question
            regions.append(AnswerRegion(
                problem=problem, group=group, rect=tuple(answer), points=DEFAULT_POINTS, page=page,
                question_rect=tuple(q["question_rect"]) if q.get("question_rect") else None))
            seen.add(key)
            added += 1
        if name_rect is None and layout.get("name_rect"):
            name_rect, name_page = tuple(layout["name_rect"]), page
        if id_rect is None and layout.get("id_rect"):
            id_rect, id_page = tuple(layout["id_rect"]), page
    if replace:   # kept regions of unprocessed pages + new ones, in page order
        regions.sort(key=lambda r: r.page)
    return regions, name_rect, name_page, added, id_rect, id_page

def job_layout(page_paths, existing, replace, columns=0, worker=None):
    """Detect regions on every page (no LaTeX). Phase 1 of auto-setup."""
    llm = _client(worker)
    pages = _Pages(page_paths)
    layouts = detect_pages(llm, pages, worker, columns=columns)
    if not layouts:            # stopped before the first page finished
        return None
    result = regions_from_layouts(layouts, existing, replace)
    worker.progress.emit(len(page_paths), len(page_paths), "Done")
    return result

STEPS = ["Detect regions & questions", "Check boxes, set points", "Generate rubrics", "Check rubrics"]
STEPS_NO_LLM = ["Draw answer boxes", "Set points", "Write rubrics", "Check rubrics"]
SUBTITLE = ("Upload the blank exam, let the LLM find the questions, check the boxes and points, then "
            "generate rubrics. Everything is saved automatically.")
SUBTITLE_NO_LLM = ("Upload the blank exam, draw the answer boxes and the name field, set the points and "
                   "write the rubrics (or import them). Everything is saved automatically.")

class TemplateEditorView(QWidget):
    def __init__(self):
        super().__init__()
        body, actions = setup_page(self, "Exams & Templates", SUBTITLE)
        self.t: Optional[TestTemplate] = None
        self.t_img = None            # image of the current page
        self.page = 0
        self._page_imgs: Dict[str, object] = {}
        self.sel: Optional[int] = None
        self._redraw_uid: Optional[str] = None   # "Redraw box": the question whose new answer box is drawn next
        self.dirty = False
        self._loading = False
        self._copy_src_available = False
        self.worker = None
        self._ai_tid = None
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(AUTOSAVE_MS)
        self._autosave.timeout.connect(lambda: self.save(quiet=True))

        self.lbl_saved = label("", "muted")
        actions.addWidget(self.lbl_saved)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        body.addWidget(split, 1)

        # ---- left: exams/versions ----
        left, ll = card()
        ll.addWidget(label("Exams", "sectionTitle"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setMinimumHeight(140)
        ll.addWidget(self.tree, 1)
        self.btn_new_exam = button("New exam…", "primary",
                                   "Pick the blank exam: a (multi-page) PDF or one image per page, in order")
        self.btn_add_version = button("Add version…", tooltip="Add another version (A, B, C…) of the selected exam")
        self.btn_exam_course = button("Copy / move to course…", "ghost",
                                      "Copy the selected exam to another course (e.g. another section giving the "
                                      "same test), or move it there with its scanned tests")
        self.btn_del_version = button("Delete version", "danger")
        for b in (self.btn_new_exam, self.btn_add_version, self.btn_exam_course, self.btn_del_version):
            ll.addWidget(b)
        # AI-only settings of the exam (hidden when Settings → "Use an LLM" is off)
        self.ai_box = QWidget()
        al = QVBoxLayout(self.ai_box); al.setContentsMargins(0, 6, 0, 0); al.setSpacing(ll.spacing())
        ll.addWidget(self.ai_box)
        al.addWidget(label("Grading instructions", "sectionTitle"))
        self.lbl_instr_scope = label("Whole exam: every version and question.", "muted")
        self.lbl_instr_scope.setWordWrap(True)
        al.addWidget(self.lbl_instr_scope)
        srow = QHBoxLayout()
        srow.addWidget(label("Strictness"))
        self.cb_strictness = QComboBox()
        for lvl in range(STRICTNESS_MAX, STRICTNESS_MIN - 1, -1):
            self.cb_strictness.addItem(f"{lvl} · {STRICTNESS_NAMES[lvl]}", lvl)
            self.cb_strictness.setItemData(self.cb_strictness.count() - 1, STRICTNESS_TEXT[lvl],
                                           Qt.ItemDataRole.ToolTipRole)
        self.cb_strictness.setToolTip("How strictly the AI grades this exam (every version and question): "
                                      "10 follows the rubric strictly, 5 is very generous. Your instructions "
                                      "below always take priority.")
        srow.addWidget(self.cb_strictness, 1)
        al.addLayout(srow)
        self.in_instructions = QPlainTextEdit()
        self.in_instructions.setPlaceholderText(
            "e.g. Make sure all work is shown; drop points for answers without supporting work.")
        self.in_instructions.setToolTip("Added to every grading prompt for this exam and used when "
                                        "generating rubrics.")
        self.in_instructions.setMinimumHeight(70)
        self.in_instructions.setMaximumHeight(130)
        al.addWidget(self.in_instructions)
        self.lbl_instr_hint = label("", "muted")
        self.lbl_instr_hint.setWordWrap(True)
        self.lbl_instr_hint.setStyleSheet(f"color: {theme.WARN};")
        self.lbl_instr_hint.setVisible(False)
        al.addWidget(self.lbl_instr_hint)
        self._instr_exam: Optional[str] = None
        self._instr_dirty = False
        self._instr_timer = QTimer(self)
        self._instr_timer.setSingleShot(True)
        self._instr_timer.setInterval(AUTOSAVE_MS)
        self._instr_timer.timeout.connect(self._save_instructions)

        ll.addSpacing(6)
        ll.addWidget(label("Scans & layout", "sectionTitle"))
        self.chk_double = QCheckBox("Double-sided scans")
        self.chk_double.setToolTip("Every page is followed by its back side in the scan, so a 4-page test "
                                   "is 8 scanned pages. Back sides are checked for extra work.")
        ll.addWidget(self.chk_double)
        lrow = QHBoxLayout()
        self.lbl_layout = label("", "muted")
        self.lbl_layout.setWordWrap(True)
        lrow.addWidget(self.lbl_layout, 1)
        self.btn_layout = button("Edit…", "ghost", "Insert or remove blank pages anywhere in the scanned order")
        lrow.addWidget(self.btn_layout)
        ll.addLayout(lrow)
        self.columns_box = QWidget()      # only helps the LLM find the regions
        crow = QHBoxLayout(self.columns_box); crow.setContentsMargins(0, 0, 0, 0)
        crow.addWidget(label("Question columns"))
        self.cb_columns = QComboBox()
        self.cb_columns.addItem("Auto", 0)
        for n in (1, 2, 3):
            self.cb_columns.addItem(str(n), n)
        self.cb_columns.setToolTip("Optional: how many columns of questions the pages have. Helps the LLM "
                                   "find the regions and number them column by column.")
        crow.addWidget(self.cb_columns); crow.addStretch()
        ll.addWidget(self.columns_box)
        left.setMinimumWidth(230)
        split.addWidget(left)

        # ---- center: canvas ----
        center, cl = card(margins=10)
        tb = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        tb.addWidget(label("Draw:", "muted"))
        for mode, text, tip in [(MODE_ANSWER, "Answer", "Drag to add a new answer box"),
                                (MODE_QUESTION, "Question", "Drag to set where the selected region's question is printed"),
                                (MODE_NAME, "Name", "Drag to set where students write their name"),
                                (MODE_ID, "ID", "Drag to set where students write their student ID "
                                                "(matched exactly against the roster before the name)"),
                                (MODE_VERSION, "Version", "Drag around where the exam says its version "
                                                          "('Version B', or letters students circle). Read to "
                                                          "tell versions apart; draw it on every version.")]:
            b = button(text, tooltip=tip)
            b.setCheckable(True)
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(text) + 36)
            self.mode_group.addButton(b, mode)
            tb.addWidget(b)
        self.mode_group.button(MODE_ANSWER).setChecked(True)
        tb.addStretch()
        self.lbl_name_status = label("", "muted")    # shown next to the hint under the page
        # zoom lives under the page (with the hint): the Draw row is full with five modes
        self.btn_zoom_out = button("−"); self.btn_fit = button("Fit"); self.btn_zoom_in = button("+")
        for b in (self.btn_zoom_out, self.btn_fit, self.btn_zoom_in):
            b.setFixedWidth(44 if b is not self.btn_fit else 52)
        cl.addLayout(tb)
        self.page_bar_w = QWidget()
        self.page_bar = QHBoxLayout(self.page_bar_w)
        self.page_bar.setContentsMargins(0, 0, 0, 0)
        self.page_bar.setSpacing(4)
        self.page_group = QButtonGroup(self)
        self.page_group.idClicked.connect(self.set_page)
        cl.addWidget(self.page_bar_w)
        self.viewer = PageViewer()
        cl.addWidget(self.viewer, 1)
        self.lbl_hint = label("Create or select an exam version to start.", "muted")
        self.lbl_hint.setWordWrap(True)
        hint_row = QHBoxLayout()
        hint_row.addWidget(self.lbl_hint, 1)
        status_col = QVBoxLayout()
        status_col.addWidget(self.lbl_name_status, 0, Qt.AlignmentFlag.AlignRight)
        zoom_row = QHBoxLayout()
        zoom_row.addStretch()
        for b in (self.btn_zoom_out, self.btn_fit, self.btn_zoom_in):
            zoom_row.addWidget(b)
        status_col.addLayout(zoom_row)
        hint_row.addLayout(status_col)
        cl.addLayout(hint_row)
        split.addWidget(center)

        # ---- right: steps + regions ----
        right, rl = card()
        self.lbl_steps = label("")
        self.lbl_steps.setWordWrap(True)
        self.lbl_steps.setTextFormat(Qt.TextFormat.RichText)
        rl.addWidget(self.lbl_steps)
        self.btn_autosetup = button("Auto-setup with LLM", "primary",
                                    "Step 1: find every question, its answer space and the name field on all pages, "
                                    "then transcribe each question to LaTeX.")
        rl.addWidget(self.btn_autosetup)

        hrow = QHBoxLayout()
        hrow.addWidget(label("Regions", "sectionTitle")); hrow.addStretch()
        self.btn_detect = button("Detect boxes only", "ghost",
                                 "Let the LLM find every question, its answer space and the name field on all "
                                 "pages (no LaTeX). Boxes are snapped to the page; check and adjust them.")
        hrow.addWidget(self.btn_detect)
        rl.addLayout(hrow)
        # phase-1 → phase-2 confirmation (non-modal): LaTeX only after the user checked the boxes
        self.confirm_banner = QFrame(); self.confirm_banner.setObjectName("banner")
        bl = QVBoxLayout(self.confirm_banner); bl.setContentsMargins(10, 8, 10, 8); bl.setSpacing(6)
        msg = label("Regions found. Check the boxes (move, redraw or delete them) and set the "
                              "points. Then read the questions as LaTeX.")
        msg.setWordWrap(True)
        bl.addWidget(msg)
        brow = QHBoxLayout()
        # short text: with "Not now" beside it the row must fit the panel's minimum width
        self.btn_continue_latex = button("Read questions (LaTeX)", "primary")
        self.btn_not_now = button("Not now", "ghost")
        brow.addWidget(self.btn_continue_latex); brow.addWidget(self.btn_not_now); brow.addStretch()
        bl.addLayout(brow)
        self.confirm_banner.setVisible(False)
        self._latex_pending_tid: Optional[int] = None
        rl.addWidget(self.confirm_banner)
        self.region_list = QListWidget()
        self.region_list.setMinimumHeight(120)
        self.region_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.region_list.setWordWrap(True)
        self.region_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.region_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.region_list.setToolTip("Questions in grading and feedback order. Drag one to move it.")
        self.region_list.model().rowsMoved.connect(lambda *_: self._regions_dragged())
        rl.addWidget(self.region_list, 1)
        rrow = QHBoxLayout()
        self.btn_region_up = button("↑", tooltip="Move the selected question up (or drag it in the list)")
        self.btn_region_down = button("↓", tooltip="Move the selected question down (or drag it in the list)")
        for b in (self.btn_region_up, self.btn_region_down):
            b.setFixedWidth(40)
        self.btn_sort_regions = button("Sort by number", "ghost",
                                       "Put the questions in the order of their numbers: 1, 2a, 2b, 3… "
                                       "(questions without a number keep their place at the end)")
        self.btn_del_region = button("Delete", "danger",
                                     "Delete the selected question with its box, points, question and rubric (Del)")
        rrow.addWidget(self.btn_region_up); rrow.addWidget(self.btn_region_down)
        rrow.addWidget(self.btn_sort_regions); rrow.addStretch(); rrow.addWidget(self.btn_del_region)
        rl.addLayout(rrow)
        crow2 = QHBoxLayout()
        self.btn_redraw_box = button("Redraw box", "ghost",
                                     "The selected question's answer box is wrong: remove it and drag a new one "
                                     "on any page. Its points, question and rubric stay.")
        self.btn_redraw_box.setCheckable(True)
        self.btn_copy_boxes = button("Copy boxes from another version", "ghost",
                                     "Start this version with the boxes, points and name/ID fields of another "
                                     "version of the same exam")
        crow2.addWidget(self.btn_redraw_box); crow2.addStretch(); crow2.addWidget(self.btn_copy_boxes)
        rl.addLayout(crow2)
        krow = QHBoxLayout()
        self.btn_import_key = button("Import answer key…", "ghost",
                                     "Questions (LaTeX), points, answers and rubrics from a JSON answer key "
                                     "written from the exam's .tex — matched to the boxes by label")
        self.btn_key_help = button("How to make one", "ghost",
                                   "Install the Claude Code skill that writes answer keys from a .tex exam")
        self.btn_import_tex = button("Import .tex…", "ghost",
                                     "Questions (LaTeX), points and, if the source has them, solutions read "
                                     "straight from the exam's .tex (exam class or enumerate lists) — no LLM")
        # two rows: three buttons side by side made the panel wider than the window
        krow.addWidget(self.btn_import_tex); krow.addStretch(); krow.addWidget(self.btn_import_key)
        rl.addLayout(krow)
        hrow = QHBoxLayout()
        hrow.addStretch(); hrow.addWidget(self.btn_key_help)
        rl.addLayout(hrow)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.in_problem = QLineEdit()
        self.in_group = QLineEdit()
        self.in_points = QDoubleSpinBox(); self.in_points.setRange(0, 1000); self.in_points.setDecimals(1)
        form.addRow("Problem", self.in_problem)
        form.addRow("Group", self.in_group)
        form.addRow("Max points", self.in_points)
        rl.addLayout(form)

        # main question of a part ('1b' -> question 1): shared stem, stored once per exam version
        self.stem_box = QWidget()
        sl = QVBoxLayout(self.stem_box); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)
        self.lbl_stem = label("Main question", "sectionTitle")
        sl.addWidget(self.lbl_stem)
        self.in_stem = QPlainTextEdit()
        self.in_stem.setPlaceholderText("Shared text of the main question (extracted with its first part, or type it).")
        self.in_stem.setToolTip("Sent together with every part of this question when grading and writing rubrics.")
        self.in_stem.setMinimumHeight(60)
        self.in_stem.setMaximumHeight(110)
        sl.addWidget(self.in_stem)
        self.lbl_stem_ctx = label("", "muted")
        self.lbl_stem_ctx.setWordWrap(True)
        sl.addWidget(self.lbl_stem_ctx)
        self.stem_box.setVisible(False)
        rl.addWidget(self.stem_box)

        qrow = QHBoxLayout()
        self.lbl_question = label("Question (LaTeX)", "sectionTitle")
        qrow.addWidget(self.lbl_question); qrow.addStretch()
        self.btn_extract = button("Extract from image", "ghost",
                                  "The LLM transcribes the question area as LaTeX. "
                                  "Edit it if needed; it stays fixed for grading.")
        qrow.addWidget(self.btn_extract)
        rl.addLayout(qrow)
        self.in_question = QPlainTextEdit()
        self.in_question.setPlaceholderText("Draw a question area (or the answer box is used), then Extract — or type the question.")
        rl.addWidget(self.in_question, 1)

        self.notes_box = QWidget()        # notes for the AI grader (hidden without an LLM)
        nl = QVBoxLayout(self.notes_box); nl.setContentsMargins(0, 0, 0, 0); nl.setSpacing(rl.spacing())
        nl.addWidget(label("Notes for the grader", "sectionTitle"))
        self.in_notes = QPlainTextEdit()
        self.in_notes.setPlaceholderText("Only for this question, e.g. accept answers in degrees or radians.")
        self.in_notes.setToolTip("Added to the grading prompt for this question and used when generating its rubric.")
        self.in_notes.setMinimumHeight(56)
        self.in_notes.setMaximumHeight(80)
        nl.addWidget(self.in_notes)
        rl.addWidget(self.notes_box)

        rbrow = QHBoxLayout()
        rbrow.addWidget(label("Rubric", "sectionTitle")); rbrow.addStretch()
        self.btn_rubric = button("Generate with LLM", "ghost",
                                 "The LLM writes the rubric items from the fixed question and max points.")
        rbrow.addWidget(self.btn_rubric)
        rl.addLayout(rbrow)
        self.lbl_answer_key = label("", "muted")
        self.lbl_answer_key.setWordWrap(True)
        self.lbl_answer_key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_answer_key.setVisible(False)
        rl.addWidget(self.lbl_answer_key)
        self.rubric_editor = RubricEditor()
        self.rubric_editor.setToolTip("")
        rl.addWidget(self.rubric_editor, 1)

        self.btn_extract_all = button("Extract all missing questions",
                                      tooltip="Transcribe every region without LaTeX")
        self.btn_rubric_all = button("Generate all missing rubrics",
                                     tooltip="Step 3: write a rubric for every region with points but no rubric "
                                             "(extracts the question first if needed)")
        self.btn_rubric_redo = button("Redo all rubrics from scratch", "danger",
                                      "Throw away every rubric and write new ones from the questions, points "
                                      "and grading instructions (e.g. after changing the instructions)")
        rl.addWidget(self.btn_extract_all); rl.addWidget(self.btn_rubric_all); rl.addWidget(self.btn_rubric_redo)
        prow = QHBoxLayout()
        self.ai_progress = QProgressBar(); self.ai_progress.setVisible(False)
        self.btn_stop = button("Stop", "danger", "Stop the LLM. Everything finished so far is kept.")
        self.btn_stop.setVisible(False)
        prow.addWidget(self.ai_progress, 1); prow.addWidget(self.btn_stop)
        rl.addLayout(prow)
        self.ai_status = label("", "muted")
        self.ai_status.setWordWrap(True)
        rl.addWidget(self.ai_status)
        self.in_question.setMinimumHeight(90)
        self.rubric_editor.setMinimumHeight(300)
        # scrollable so the form keeps its natural size on small screens
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setWidget(right)
        right_scroll.setMinimumWidth(360)
        split.addWidget(right_scroll)
        split.setSizes([230, 800, 400])

        # ---- signals ----
        self.btn_new_exam.clicked.connect(self.new_exam)
        self.btn_add_version.clicked.connect(self.add_version)
        self.btn_exam_course.clicked.connect(self.exam_to_course)
        self.btn_del_version.clicked.connect(self.delete_version)
        self.tree.itemClicked.connect(self.on_tree_clicked)
        self.mode_group.idClicked.connect(self.on_mode_changed)
        self.btn_zoom_in.clicked.connect(lambda: self.viewer.zoom(1.25))
        self.btn_zoom_out.clicked.connect(lambda: self.viewer.zoom(0.8))
        self.btn_fit.clicked.connect(self.viewer.fit_width)
        self.viewer.canvas.rectDrawn.connect(self.on_rect_drawn)
        self.viewer.canvas.overlayClicked.connect(self.on_overlay_clicked)
        self.viewer.canvas.overlayEdited.connect(self.on_overlay_edited)
        self.in_stem.textChanged.connect(self._stem_edited)
        self.region_list.currentRowChanged.connect(self.select_region)
        self.btn_del_region.clicked.connect(self.delete_region)
        self.btn_redraw_box.clicked.connect(lambda on: self.redraw_box() if on else self.cancel_redraw())
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel_redraw,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.btn_region_up.clicked.connect(lambda: self.move_region(-1))
        self.btn_region_down.clicked.connect(lambda: self.move_region(1))
        self.btn_sort_regions.clicked.connect(self.sort_regions)
        QShortcut(QKeySequence.StandardKey.Delete, self.region_list, activated=self.delete_region,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=lambda: self.save(),
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.in_problem.textEdited.connect(lambda v: self._set_field("problem", v.strip()))
        self.in_group.textEdited.connect(lambda v: self._set_field("group", v.strip()))
        self.in_points.valueChanged.connect(lambda v: self._set_field("points", float(v)))
        self.in_question.textChanged.connect(lambda: self._set_field("question_text", self.in_question.toPlainText()))
        self.rubric_editor.changed.connect(self._rubric_edited)
        self.in_notes.textChanged.connect(lambda: self._set_field("grading_notes", self.in_notes.toPlainText()))
        self.in_instructions.textChanged.connect(self._instructions_edited)
        self.cb_strictness.currentIndexChanged.connect(lambda _: self._strictness_changed())
        self.btn_extract.clicked.connect(lambda: self.run_ai("extract", selected_only=True))
        self.btn_rubric.clicked.connect(lambda: self.run_ai("rubric", selected_only=True))
        self.btn_extract_all.clicked.connect(lambda: self.run_ai("extract"))
        self.btn_rubric_all.clicked.connect(lambda: self.run_ai("rubric"))
        self.btn_rubric_redo.clicked.connect(lambda: self.run_ai("rubric", redo=True))
        self.btn_copy_boxes.clicked.connect(self.copy_boxes_from_version)
        self.btn_import_key.clicked.connect(lambda: self.import_answer_key())
        self.btn_import_tex.clicked.connect(lambda: self.import_answer_key(tex=True))
        self.btn_key_help.clicked.connect(self.answer_key_help)
        self.btn_detect.clicked.connect(lambda: self.run_detect(autosetup=False))
        self.btn_autosetup.clicked.connect(lambda: self.run_detect(autosetup=True))
        self.btn_stop.clicked.connect(self.stop_ai)
        self.btn_continue_latex.clicked.connect(self.continue_latex)
        self.btn_not_now.clicked.connect(self.dismiss_latex_banner)
        self.chk_double.toggled.connect(self.set_double_sided)
        self.btn_layout.clicked.connect(lambda: ScanLayoutDialog(self, self).exec())
        self.cb_columns.currentIndexChanged.connect(lambda _: self.set_columns(self.cb_columns.currentData()))

        self.refresh_list()
        self.apply_llm_mode()

    def apply_llm_mode(self):
        """Settings → "Use an LLM" off: the LLM actions and the AI-only settings (grading
        instructions, strictness, columns, notes) are hidden; boxes, questions and rubrics are
        drawn, typed or imported."""
        on = llm_enabled()
        # question areas only tell the LLM where to read the question: hand grading needs the answer box
        q_mode = self.mode_group.button(MODE_QUESTION)
        for w in (self.btn_autosetup, self.btn_detect, self.btn_extract, self.btn_rubric, self.btn_extract_all,
                  self.btn_rubric_all, self.btn_rubric_redo, self.ai_box, self.columns_box, self.notes_box, q_mode):
            w.setVisible(on)
        if not on:
            self.confirm_banner.setVisible(False)
            if q_mode.isChecked():
                self.mode_group.button(MODE_ANSWER).setChecked(True)
                self.on_mode_changed(MODE_ANSWER)
        self._refresh_overlays()
        sub = self.findChild(QLabel, "pageSubtitle")
        if sub is not None:
            sub.setText(SUBTITLE if on else SUBTITLE_NO_LLM)
        self.in_question.setPlaceholderText(
            "Draw a question area (or the answer box is used), then Extract — or type the question." if on else
            "Optional: type the question (LaTeX), or import it with Import .tex… / Import answer key….")
        self._update_enabled()

    # ---------- exam / version list ----------
    def refresh_list(self, select_id=None):
        select_id = select_id or (self.t.id if self.t else None)
        self.tree.clear()
        exams = {}
        for t in list_templates():
            parent = exams.get(t.exam_name)
            if parent is None:
                parent = QTreeWidgetItem([t.exam_name])
                f = parent.font(0); f.setBold(True); parent.setFont(0, f)
                parent.setData(0, Qt.ItemDataRole.UserRole, ("exam", t.exam_name))
                self.tree.addTopLevelItem(parent)
                exams[t.exam_name] = parent
            pages = f" · {t.page_count} p" if t.page_count > 1 else ""
            child = QTreeWidgetItem([f"Version {t.version_label}  ·  {len(t.regions)} Q{pages}"])
            child.setToolTip(0, f"{t.page_count} page(s), {len(t.regions)} regions"
                             + (", name field set" if t.name_rect else ", no name field"))
            child.setData(0, Qt.ItemDataRole.UserRole, ("version", t.id))
            parent.addChild(child)
            if t.id == select_id:
                self.tree.setCurrentItem(child)
        self.tree.expandAll()

    # ---------- exam/version shared with the other tabs (main window) ----------
    def context(self):
        return (self.t.exam_name, self.t.id) if self.t is not None else (None, None)

    def set_context(self, exam, tid=None):
        """Open the version another tab was on (that exam's first version if it gave none).
        Nothing changes while the LLM works on the open version."""
        if not exam or self.worker is not None:
            return
        if self.t is not None and self.t.exam_name == exam and tid in (None, self.t.id):
            return
        versions = list_versions(exam)
        t = next((v for v in versions if v.id == tid), None) or (versions[0] if versions else None)
        if t is None or (self.t is not None and self.t.id == t.id):
            return
        self._confirm_discard()
        self.load(t.id)
        self._select_in_tree(t.id)

    def _select_in_tree(self, tid):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                kind, val = top.child(j).data(0, Qt.ItemDataRole.UserRole)
                if kind == "version" and val == tid:
                    self.tree.setCurrentItem(top.child(j))
                    return

    def _selected_exam(self) -> Optional[str]:
        it = self.tree.currentItem()
        if not it:
            return self.t.exam_name if self.t else None
        kind, val = it.data(0, Qt.ItemDataRole.UserRole)
        if kind == "exam":
            return val
        return it.parent().data(0, Qt.ItemDataRole.UserRole)[1]

    def on_tree_clicked(self, item, _col):
        kind, val = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "version" and (not self.t or self.t.id != val):
            self._confirm_discard()
            self.load(val)

    def _confirm_discard(self) -> bool:
        """Autosave makes this a flush: pending changes are written, nothing is ever discarded."""
        self.flush()
        return True

    def flush(self):
        if self._autosave.isActive():
            self._autosave.stop()
        if self.dirty:
            self.save(quiet=True)
        self._save_instructions()

    # ---------- exam-wide grading instructions ----------
    def _load_instructions(self, exam: Optional[str]):
        """Show the instructions of `exam` (shared by all its versions)."""
        if exam == self._instr_exam:
            return
        self._save_instructions()          # never lose edits to the previous exam
        self._instr_exam = exam
        self._loading = True
        self.in_instructions.setPlainText(get_exam_instructions(exam) if exam else "")
        self.cb_strictness.setCurrentIndex(max(0, self.cb_strictness.findData(
            get_exam_strictness(exam) if exam else LLM_CONFIG.get("default_strictness", 10))))
        self._loading = False
        self.lbl_instr_hint.setVisible(False)
        self.lbl_instr_scope.setText(f"Whole exam “{exam}”: every version and question." if exam
                                     else "Whole exam: every version and question.")

    def _instructions_edited(self):
        if self._loading or not self._instr_exam:
            return
        self._instr_dirty = True
        self.lbl_saved.setText("Saving…")
        self._instr_timer.start()
        if self.t and any(r.rubric for r in self.t.regions):
            self.lbl_instr_hint.setText("Instructions changed — regenerate rubrics to include them "
                                        "(grading already uses them).")
            self.lbl_instr_hint.setVisible(True)

    def _strictness_changed(self):
        """Saved at once (a single choice, nothing to debounce); applies to questions graded from now on."""
        if self._loading or not self._instr_exam:
            return
        set_exam_strictness(self._instr_exam, self.cb_strictness.currentData())
        self.lbl_saved.setText("Saved ✓")

    def _save_instructions(self):
        self._instr_timer.stop()
        if not self._instr_dirty or not self._instr_exam:
            return
        set_exam_instructions(self._instr_exam, self.in_instructions.toPlainText())
        self._instr_dirty = False
        if not self.dirty:
            self.lbl_saved.setText("Saved ✓")

    def current_instructions(self) -> str:
        return self.in_instructions.toPlainText() if self._instr_exam else ""

    def _pick_pages(self, title) -> List[str]:
        """A multi-page PDF, or one or more images (one per page, in the selected order)."""
        files, _ = QFileDialog.getOpenFileNames(self, title, "", IMAGE_FILTER)
        pages = []
        for f in files:
            pages.extend(import_pages(f, "templates"))
        return pages

    def new_exam(self):
        self.flush()
        name, ok = QInputDialog.getText(self, "New exam", "Exam name (e.g. Midterm 1):")
        if not ok or not name.strip():
            return
        try:
            pages = self._pick_pages("Blank exam (PDF or page images, in order)")
            if not pages:
                return
            self.create_exam(name.strip(), pages)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def create_exam(self, name: str, pages: List[str]) -> TestTemplate:
        t = create_template_from_image(f"{name} vA", pages, name, "A")
        t.id = persist(t)
        default = (LLM_CONFIG.get("default_instructions") or "").strip()
        if default and not get_exam_instructions(name).strip():
            set_exam_instructions(name, default)
        set_exam_strictness(name, LLM_CONFIG.get("default_strictness", 10))
        self.refresh_list(t.id)
        self.load(t.id)
        return t

    def add_version(self):
        exam = self._selected_exam()
        if not exam:
            QMessageBox.information(self, "Add version", "Select an exam first.")
            return
        self.flush()
        versions = list_versions(exam)
        used = {v.version_label for v in versions}
        default = next((c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in used), "")
        label_, ok = QInputDialog.getText(self, "Add version", f"Version label for '{exam}':", text=default)
        if not ok or not label_.strip():
            return
        if label_.strip() in used:
            QMessageBox.warning(self, "Add version", "That version label already exists."); return
        try:
            pages = self._pick_pages(f"Blank pages for version {label_.strip()}")
            if not pages:
                return
            src = self.t if self.t and self.t.exam_name == exam else (versions[0] if versions else None)
            if src is not None and len(pages) != src.page_count:
                QMessageBox.warning(self, "Add version",
                                    f"Version {src.version_label} has {src.page_count} page(s), the new one has "
                                    f"{len(pages)}. Tests are split by page count, so versions should match.")
            # the outline (boxes, labels, points, name/ID fields, scan layout) is always copied;
            # the text is only kept if the questions are the same
            keep_text = False
            if src is not None and any(r.question_text.strip() or r.rubric.strip() for r in src.regions):
                box = QMessageBox(QMessageBox.Icon.Question, "New version",
                                  f"The boxes and points of version {src.version_label} are copied to version "
                                  f"{label_.strip()} (adjust them if needed).\n\n"
                                  f"Does version {label_.strip()} have the same questions? Then the LaTeX and "
                                  "rubrics are copied too; otherwise they are read again from the new pages.",
                                  parent=self)
                b_boxes = box.addButton("Boxes only", QMessageBox.ButtonRole.AcceptRole)
                b_all = box.addButton("Same questions: copy text too", QMessageBox.ButtonRole.YesRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.setDefaultButton(b_boxes)
                box.exec()
                if box.clickedButton() not in (b_boxes, b_all):
                    return
                keep_text = box.clickedButton() is b_all
            self.create_version(exam, label_.strip(), pages, src, keep_text=keep_text)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def create_version(self, exam: str, label_: str, pages: List[str], copy_from: Optional[TestTemplate] = None,
                       keep_text: bool = True):
        if copy_from is not None:
            t = duplicate_as_version(copy_from, pages, label_, keep_text=keep_text)
        else:
            t = create_template_from_image(f"{exam} v{label_}", pages, exam, label_)
        t.id = persist(t)
        self.refresh_list(t.id)
        self.load(t.id)
        return t

    def delete_version(self):
        """Delete the open version. The exam's last version takes the exam's scanned tests and
        grades with it (after a warning; refused while a background job runs)."""
        if not self.t:
            return
        exam = self.t.exam_name
        last = not any(v.id != self.t.id for v in list_versions(exam, self.t.course_id))
        n = exam_test_count(exam, self.t.course_id) if last else 0
        Yes, Cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
        if n:
            job = getattr(self.window(), "busy_job", lambda: None)()
            if job:
                QMessageBox.information(self, "Delete version", f"{job}. Stop it (or let it finish) before "
                                                                f"deleting “{exam}”.")
                return
            if QMessageBox.warning(self, "Delete version",
                                   f"{self.t.display_name} is the last version of “{exam}”: deleting it deletes "
                                   f"the exam, and its {n} scanned test{'s' if n != 1 else ''} and their grades "
                                   f"are deleted with it. This can't be undone.\n\nDelete it?",
                                   Yes | Cancel, Cancel) != Yes:
                return
        elif QMessageBox.question(self, "Delete version",
                                  f"Delete {self.t.display_name}? Graded submissions are kept but lose their template."
                                  ) != Yes:
            return
        self._autosave.stop()
        delete_template(self.t.id)
        self._close_open()
        self.refresh_list()

    def _close_open(self):
        """Nothing open (the version was deleted, moved, or belongs to another course)."""
        self._autosave.stop()
        self.t, self.t_img, self.sel, self.dirty = None, None, None, False
        self.viewer.set_image(None)
        self.viewer.canvas.set_overlays([])
        self.region_list.clear()
        self._rebuild_page_bar()
        self._load_instructions(None)
        self._sync_layout_controls()
        self._update_enabled()

    def course_changed(self):
        """The sidebar switched course (the main window saved pending edits first): show its
        exams and open the first one."""
        self._close_open()
        self.refresh_list()
        first = list_templates()
        if first:
            self.load(first[0].id)
            self._select_in_tree(first[0].id)

    def exam_to_course(self):
        """Copy the selected exam (versions, boxes, questions, rubrics; no scans) to another
        course, or move it there with its scanned tests and grades."""
        exam = self._selected_exam()
        here = current_course()
        others = [c for c in list_courses() if c["id"] != here]
        if not exam:
            return
        if not others:
            QMessageBox.information(self, "Copy / move exam", "There is no other course yet: add one with "
                                                              "“Manage courses…” in the sidebar.")
            return
        self.flush()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Copy or move “{exam}”")
        form = QFormLayout(dlg)
        cb_course = QComboBox()
        for c in others:
            cb_course.addItem(c["name"], c["id"])
        cb_mode = QComboBox()
        cb_mode.addItem("Copy (versions, boxes, questions, rubrics; no scans)", "copy")
        cb_mode.addItem("Move (with its scanned tests and grades)", "move")
        in_name = QLineEdit(exam)
        form.addRow("To course", cb_course)
        form.addRow("Action", cb_mode)
        form.addRow("Name there", in_name)
        cb_mode.currentIndexChanged.connect(lambda _: in_name.setEnabled(cb_mode.currentData() == "copy"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.copy_or_move_exam(exam, cb_course.currentData(), cb_mode.currentData(), in_name.text())

    def copy_or_move_exam(self, exam: str, to_course: int, mode: str, new_name: str = "") -> bool:
        here = current_course()
        try:
            if mode == "move":
                move_exam(exam, here, to_course)
            else:
                copy_exam(exam, here, to_course, new_name or exam)
        except ValueError as e:
            QMessageBox.warning(self, "Copy / move exam", str(e))
            return False
        where = course_name(to_course)
        if mode == "move":
            if self.t is not None and self.t.exam_name == exam:
                self._close_open()
            self.refresh_list()
            self._status(f"Moved “{exam}” to {where}.")
        else:
            self._status(f"Copied “{exam}” to {where} as “{(new_name or exam).strip()}”. Pick that course in the "
                         "sidebar to see it.")
        return True

    def _status(self, msg: str):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(msg, 8000)

    def _page_image(self, i: int):
        path = self.t.pages[i]
        if path not in self._page_imgs:
            self._page_imgs[path] = read_image(path)
        return self._page_imgs[path]

    def load(self, tid: int):
        t = load_template(tid)
        if t is None:
            return
        self.t = t
        missing = [p for i, p in enumerate(t.pages) if self._page_image(i) is None]
        if missing:
            QMessageBox.critical(self, "Error", "Cannot read template page(s):\n" + "\n".join(missing))
            self.t = None
            self._update_enabled()
            return
        self.dirty = False
        self.sel = None
        self._redraw_uid = None
        self._copy_src_available = bool(self._copy_sources())
        self.viewer.canvas.active_key = None
        self._show_stem()
        self._load_instructions(t.exam_name)
        self.page, self.t_img = 0, None
        self._rebuild_page_bar()
        self.set_page(0)
        self._rebuild_region_list()
        if t.regions:
            self.region_list.setCurrentRow(0)
        self._sync_layout_controls()
        self.lbl_saved.setText("Saved ✓")
        self._update_enabled()
        fixed = repair_part_json(t)   # raw JSON / damaged LaTeX / looping text from older versions
        if fixed:
            self._rebuild_region_list()
            if t.regions:
                self.region_list.setCurrentRow(0)
                self.select_region(0)
            self._mark_dirty()
            cleared = any("cleared" in f for f in fixed)
            self.ai_status.setText("Cleaned up question text saved by an older version: " + "; ".join(fixed)
                                   + (". Use “Extract all missing questions” to read the cleared ones again."
                                      if cleared else "."))

    # ---------- scan layout (blank / back pages) and columns ----------
    def _sync_layout_controls(self):
        t = self.t
        self._loading = True
        if t is None:
            self.chk_double.setChecked(False)
            self.lbl_layout.setText("")
            self.cb_columns.setCurrentIndex(0)
        else:
            lay = t.layout
            self.chk_double.setChecked(t.page_count > 0 and lay == double_sided_layout(t.page_count))
            n, b = len(lay), t.blank_count
            text = f"{n} scanned page{'s' if n != 1 else ''} per test"
            if b:
                text += f" ({t.page_count} + {b} blank): {layout_text(lay)}"
            self.lbl_layout.setText(text)
            self.cb_columns.setCurrentIndex(max(0, self.cb_columns.findData(int(t.columns or 0))))
        self._loading = False
        self.confirm_banner.setVisible(t is not None and self._latex_pending_tid == t.id and llm_enabled())

    def _layout_changed(self):
        self._sync_layout_controls()
        self._mark_dirty()

    def set_double_sided(self, on: bool):
        if self._loading or self.t is None:
            return
        self.t.scan_layout = double_sided_layout(self.t.page_count) if on else []
        self._layout_changed()

    def insert_blank(self, after: int):
        """Insert a blank page after position `after` of the scanned order (-1 = at the start)."""
        if self.t is None:
            return
        lay = list(self.t.layout)
        lay.insert(max(0, min(after + 1, len(lay))), BLANK)
        self.t.scan_layout = lay
        self._layout_changed()

    def remove_blank(self, pos: int):
        if self.t is None:
            return
        lay = list(self.t.layout)
        if 0 <= pos < len(lay) and lay[pos] == BLANK:
            del lay[pos]
            self.t.scan_layout = lay if BLANK in lay else []
            self._layout_changed()

    def set_columns(self, n):
        if self._loading or self.t is None:
            return
        n = int(n or 0)
        if n != self.t.columns:
            self.t.columns = n
            self._mark_dirty()

    def save(self, quiet=False):
        if self.t is None:
            return
        self._autosave.stop()
        try:
            self.t.id = persist(self.t)
            self.dirty = False
            self.refresh_list(self.t.id)
            self.lbl_saved.setText("Saved ✓")
            self._update_enabled()
            if not quiet:
                self.window().statusBar().showMessage(f"Saved {self.t.display_name}", 3000)
        except Exception as e:
            self.lbl_saved.setText("Save failed")
            QMessageBox.critical(self, "Error", str(e))

    # ---------- pages ----------
    def _rebuild_page_bar(self):
        for b in self.page_group.buttons():
            self.page_group.removeButton(b)
            b.deleteLater()
        while self.page_bar.count():
            it = self.page_bar.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n = self.t.page_count if self.t else 0
        self.page_bar_w.setVisible(n > 1)
        if n > 1:
            self.page_bar.addWidget(label("Page", "muted"))
            for i in range(n):
                b = button(str(i + 1))
                b.setCheckable(True)
                b.setFixedWidth(40)
                self.page_group.addButton(b, i)
                self.page_bar.addWidget(b)
            self.page_bar.addStretch()

    def set_page(self, i: int):
        if self.t is None or not 0 <= i < self.t.page_count:
            return
        changed = i != self.page or self.t_img is None
        self.page = i
        self.t_img = self._page_image(i)
        b = self.page_group.button(i)
        if b:
            b.setChecked(True)
        if changed:
            self.viewer.set_image(self.t_img)
        self._refresh_overlays()
        self._update_enabled()

    # ---------- regions ----------
    def _region_text(self, r):
        n = len(r.rubric.items)
        marks = ("LaTeX ✓" if r.question_text.strip() else "LaTeX –") + "   " + \
                (f"Rubric ✓ {n} item{'s' if n != 1 else ''}" if n else "Rubric –") + \
                ("   Notes ✓" if r.grading_notes.strip() else "")
        page = f"p{r.page + 1}  ·  " if (self.t and self.t.page_count > 1) else ""
        return f"⠿  {page}{r.problem}  ·  {r.group}  ·  {r.points:g} pts\n     {marks}"

    def _region_item(self, r) -> QListWidgetItem:
        it = QListWidgetItem(self._region_text(r))
        it.setData(Qt.ItemDataRole.UserRole, r.uid)
        return it

    def _rebuild_region_list(self):
        self._loading = True
        self.region_list.clear()
        for r in (self.t.regions if self.t else []):
            self.region_list.addItem(self._region_item(r))
        self._loading = False

    # ---------- question order ----------
    def _grading_running(self) -> bool:
        """An LLM run is grading tests of this version (it saves grades by question position)."""
        return self.t is not None and self.t.id is not None and any(
            s.status == SUB_GRADING for s in list_submissions(template_id=self.t.id))

    def reorder_regions(self, uids: List[str]) -> bool:
        """Put the regions in the order of `uids`. Saved grades follow their question (saved with
        the template). Refused while the AI is grading this version."""
        if self.t is None or self.worker is not None:
            return False
        by_uid = {r.uid: r for r in self.t.regions}
        if sorted(uids) != sorted(by_uid) or uids == [r.uid for r in self.t.regions]:
            self._rebuild_region_list()
            return False
        if self._grading_running():
            QMessageBox.information(self, "Question order", "The AI is grading tests of this version. Stop it "
                                                            "in LLM Grade before reordering the questions.")
            self._rebuild_region_list()
            if self.sel is not None:
                self.region_list.setCurrentRow(self.sel)
            return False
        sel_uid = self.t.regions[self.sel].uid if self.sel is not None else None
        self.t.regions = [by_uid[u] for u in uids]
        self._rebuild_region_list()
        self.sel = None
        if sel_uid is not None:
            self.region_list.setCurrentRow(uids.index(sel_uid))
        self._mark_dirty()
        self._refresh_overlays()
        return True

    def _grades_lost_note(self, action: str = "") -> str:
        """Warning for actions that replace every question: grades of removed questions are deleted."""
        n = count_template_grades(self.t.id) if (self.t is not None and self.t.id) else 0
        if not n:
            return ""
        who = f"“{action}” replaces" if action else "This replaces"
        return (f"\n\n{who} the questions, so the {n} grade{'s' if n != 1 else ''} already given on tests of "
                "this version are deleted when the change is saved.")

    def _regions_dragged(self):
        if self._loading:
            return
        uids = [self.region_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.region_list.count())]
        # the list already shows the new order; apply it (or put the list back)
        QTimer.singleShot(0, lambda: self.reorder_regions(uids))

    def move_region(self, step: int):
        if self.t is None or self.sel is None:
            return
        j = self.sel + step
        if not 0 <= j < len(self.t.regions):
            return
        uids = [r.uid for r in self.t.regions]
        uids.insert(j, uids.pop(self.sel))
        self.reorder_regions(uids)

    def sort_regions(self):
        """Order by question number and part (1, 2a, 2b, 10); unnumbered ones keep their order last."""
        if self.t is None:
            return
        def key(ir):
            i, r = ir
            p = parse_label(r.problem)
            return (0, p[0], _part_key(p[1] or ""), i) if p else (1, 0, (), i)
        uids = [r.uid for _, r in sorted(enumerate(self.t.regions), key=key)]
        if not self.reorder_regions(uids):
            self.window().statusBar().showMessage("The questions are already in number order.", 3000) \
                if hasattr(self.window(), "statusBar") else None

    def _refresh_overlays(self):
        if not self.t:
            self.viewer.canvas.set_overlays([])
            self.lbl_name_status.setText("")
            return
        ovs = []
        show_questions = llm_enabled()     # question areas are only for the LLM
        for i, r in enumerate(self.t.regions):
            if r.page != self.page:
                continue
            sel = i == self.sel
            if r.uid != self._redraw_uid:  # the box being redrawn is gone: drag the new one anywhere
                ovs.append(Overlay(r.rect, theme.COLOR_SELECTED if sel else theme.COLOR_ANSWER,
                                   f"{r.problem} · {r.points:g}", sel, key=("a", i), editable=True))
            if r.question_rect and show_questions:
                ovs.append(Overlay(r.question_rect, theme.COLOR_QUESTION, f"{r.problem} question" if sel else "",
                                   sel, dashed=True, key=("q", i), editable=True))
        active = self.viewer.canvas.active_key
        if self.t.name_rect and self.t.name_page == self.page:
            ovs.append(Overlay(self.t.name_rect, theme.COLOR_NAME, "Name", active == ("n", None),
                               key=("n", None), editable=True))
        if self.t.id_rect and self.t.id_page == self.page:
            ovs.append(Overlay(self.t.id_rect, COLOR_ID, "ID", active == ("i", None),
                               key=("i", None), editable=True))
        if self.t.version_rect and self.t.version_page == self.page:
            ovs.append(Overlay(self.t.version_rect, COLOR_VERSION, "Version", active == ("v", None),
                               key=("v", None), editable=True))
        self.viewer.canvas.set_overlays(ovs)
        self.lbl_name_status.setText(self.field_status())

    def field_status(self) -> str:
        """e.g. 'Name ✓ p1 · ID ✓ p1'."""
        if self.t is None:
            return ""
        def one(name, rect, page):
            if not rect:
                return f"No {name.lower()}"
            return f"{name} ✓" + (f" p{page + 1}" if self.t.page_count > 1 else "")
        return (one("Name", self.t.name_rect, self.t.name_page) + " · " + one("ID", self.t.id_rect, self.t.id_page)
                + " · " + one("Version", self.t.version_rect, self.t.version_page))

    def current_step(self) -> int:
        """0-based index into STEPS of what the user should do next."""
        regs = self.t.regions if self.t else []
        # without an LLM the question text is optional (hand grading shows the answer itself)
        if not regs or (llm_enabled() and not all(r.question_text.strip() for r in regs)):
            return 0
        if not any(r.rubric for r in regs):
            return 1
        if not all(r.rubric for r in regs):
            return 2
        return 3

    def _render_steps(self):
        if self.t is None:
            self.lbl_steps.setText("")
            return
        cur = self.current_step()
        parts = []
        for i, s in enumerate(STEPS if llm_enabled() else STEPS_NO_LLM):
            if i < cur:
                parts.append(f"<span style='color:{theme.OK}'>✓ {s}</span>")
            elif i == cur:
                parts.append(f"<b style='color:{theme.ACCENT}'>{i + 1} {s}</b>")
            else:
                parts.append(f"<span style='color:{theme.MUTED}'>{i + 1} {s}</span>")
        self.lbl_steps.setText(" → ".join(parts))

    def _update_enabled(self):
        has_t = self.t is not None
        has_sel = has_t and self.sel is not None
        busy = self.worker is not None
        # region indices/contents must stay stable while a job runs (results are applied by index)
        for w in (self.in_problem, self.in_group, self.in_points, self.in_question, self.rubric_editor, self.in_notes):
            w.setEnabled(has_sel and not busy)
        self.in_instructions.setEnabled(has_t)
        self.cb_strictness.setEnabled(has_t)
        self.btn_del_region.setEnabled(has_sel and not busy)
        # redrawing lasts while its question stays selected in Answer mode (else it is cancelled)
        if self._redraw_uid is not None and (busy or not has_sel or self.mode_group.checkedId() != MODE_ANSWER
                                             or self.t.regions[self.sel].uid != self._redraw_uid):
            self._redraw_uid = None
            self._refresh_overlays()
        self.btn_redraw_box.setEnabled(has_sel and not busy)
        self.btn_redraw_box.setChecked(self._redraw_uid is not None)
        n_regions = len(self.t.regions) if has_t else 0
        self.btn_region_up.setEnabled(has_sel and not busy and self.sel > 0)
        self.btn_region_down.setEnabled(has_sel and not busy and self.sel < n_regions - 1)
        self.btn_sort_regions.setEnabled(has_t and not busy and n_regions > 1)
        self.region_list.setDragEnabled(has_t and not busy)
        self.viewer.canvas.draw_enabled = has_t and not busy
        self.btn_extract.setEnabled(has_sel and not busy)
        self.btn_rubric.setEnabled(has_sel and not busy and self.t.regions[self.sel].points > 0)
        self.btn_extract_all.setEnabled(has_t and not busy and bool(self.t.regions))
        self.btn_rubric_all.setEnabled(has_t and not busy and any(r.points > 0 for r in self.t.regions))
        self.btn_rubric_redo.setEnabled(has_t and not busy and any(r.rubric for r in self.t.regions))
        self.btn_copy_boxes.setVisible(has_t and self._copy_src_available)
        self.btn_copy_boxes.setEnabled(not busy)
        self.btn_import_key.setEnabled(has_t and not busy)
        self.btn_import_tex.setEnabled(has_t and not busy)
        has_rubric = has_sel and bool(self.t.regions[self.sel].rubric)
        self.btn_rubric.setText("Redo from scratch" if has_rubric else "Generate with LLM")
        self.btn_detect.setEnabled(has_t and not busy)
        self.btn_autosetup.setEnabled(has_t and not busy)
        self.btn_del_version.setEnabled(has_t)
        self.btn_new_exam.setEnabled(not busy)
        self.btn_add_version.setEnabled(not busy)
        self.btn_exam_course.setEnabled(not busy and bool(self._selected_exam()))
        self.tree.setEnabled(not busy)
        self.btn_stop.setVisible(busy)
        for w in (self.chk_double, self.btn_layout, self.cb_columns):
            w.setEnabled(has_t and not busy)
        self.btn_continue_latex.setEnabled(has_t and not busy)
        self._render_steps()
        mode = self.mode_group.checkedId()
        if not has_t:
            hint = "Create or select an exam version to start."
        elif busy:
            hint = "The LLM is working — you can look around, editing boxes is paused."
        elif self._redraw_uid is not None:
            hint = (f"Drag the new answer box of {self.t.regions[self.sel].problem} (on any page). "
                    "Its points, question and rubric stay. Esc or Redraw box again keeps the old box.")
        elif mode == MODE_ANSWER:
            hint = ("Drag on the page to add an answer box. Click a box to select it, then drag it or its "
                    "handles to move/resize (arrow keys nudge; Shift+drag draws over boxes). Ctrl+wheel zooms.")
        elif mode == MODE_QUESTION:
            hint = ("Drag around the printed question for the selected region." if has_sel
                    else "Select a region first, then drag around its printed question.")
        elif mode == MODE_ID:
            hint = "Drag around the box where students write their student ID (optional, most reliable match)."
        elif mode == MODE_VERSION:
            hint = ("Drag around where the exam states its version (e.g. 'Version B'). Scans are then sorted "
                    "by what this field says instead of by page layout. Draw it on every version.")
        else:
            hint = "Drag around the area where students write their name."
        self.lbl_hint.setText(hint)

    def _mark_dirty(self):
        self.dirty = True
        self.lbl_saved.setText("Saving…")
        self._autosave.start()       # debounced autosave
        self._render_steps()

    def on_mode_changed(self, mode):
        self.viewer.canvas.draw_color = {MODE_ANSWER: theme.COLOR_ANSWER, MODE_QUESTION: theme.COLOR_QUESTION,
                                         MODE_NAME: theme.COLOR_NAME, MODE_ID: COLOR_ID,
                                         MODE_VERSION: COLOR_VERSION}[mode]
        self._update_enabled()

    def on_rect_drawn(self, rect):
        if self.t is None or self.worker is not None:
            return
        mode = self.mode_group.checkedId()
        if mode == MODE_NAME:
            self.t.name_rect, self.t.name_page = rect, self.page
        elif mode == MODE_ID:
            self.t.id_rect, self.t.id_page = rect, self.page
        elif mode == MODE_VERSION:
            self.t.version_rect, self.t.version_page = rect, self.page
        elif mode == MODE_QUESTION:
            if self.sel is None:
                QMessageBox.information(self, "Question area", "Select the region this question belongs to first.")
                return
            r = self.t.regions[self.sel]
            if r.page != self.page:
                QMessageBox.information(self, "Question area",
                                        f"{r.problem} is on page {r.page + 1}; its question area must be on the same page.")
                return
            r.question_rect = rect
            # new question image: the old transcription no longer applies
            if r.question_text.strip() and QMessageBox.question(
                    self, "Question area", "Clear the existing LaTeX so it can be re-extracted?"
            ) == QMessageBox.StandardButton.Yes:
                self.in_question.setPlainText("")
        elif self._redraw_uid is not None and self.sel is not None:
            # "Redraw box": only the answer box changes; points, LaTeX, rubric and grades stay
            r = self.t.regions[self.sel]
            if r.page != self.page:
                r.page, r.question_rect = self.page, None   # its question area was on the old page
            r.rect = rect
            self._redraw_uid = None
            self.viewer.canvas.active_key = ("a", self.sel)
            item = self.region_list.item(self.sel)
            if item:
                item.setText(self._region_text(r))
            self._update_enabled()
        else:
            prev = self.t.regions[-1] if self.t.regions else None
            n = len(self.t.regions) + 1
            r = add_region(self.t, f"Q{n}", prev.group if prev else "default", rect, DEFAULT_POINTS)
            r.page = self.page
            self.region_list.addItem(self._region_item(r))
            self.region_list.setCurrentRow(len(self.t.regions) - 1)
        self._mark_dirty()
        self._refresh_overlays()

    def redraw_box(self):
        """Remove the selected question's answer box; the next drag (any page) is its new box."""
        if self.t is None or self.sel is None or self.worker is not None:
            self._update_enabled()
            return
        if self.mode_group.checkedId() != MODE_ANSWER:
            self.mode_group.button(MODE_ANSWER).setChecked(True)
            self.on_mode_changed(MODE_ANSWER)
        self._redraw_uid = self.t.regions[self.sel].uid
        self.viewer.canvas.active_key = None      # a drag over the old place draws, it doesn't move
        self._refresh_overlays()
        self._update_enabled()

    def cancel_redraw(self):
        """Keep the old answer box (Esc, or Redraw box clicked again)."""
        if self._redraw_uid is None:
            return
        self._redraw_uid = None
        if self.sel is not None:
            self.viewer.canvas.active_key = ("a", self.sel)
        self._refresh_overlays()
        self._update_enabled()

    def on_overlay_clicked(self, key):
        """Select the clicked box; it becomes the active box (handles to move/resize it)."""
        kind, i = key
        self.viewer.canvas.active_key = key
        if kind in ("a", "q") and i is not None and i != self.sel:
            self.region_list.setCurrentRow(i)     # refreshes the overlays
        else:
            self._refresh_overlays()

    def on_overlay_edited(self, key, rect):
        """A box was moved/resized on the canvas: write it back and autosave."""
        if self.t is None or self.worker is not None:
            return
        kind, i = key
        rect = tuple(int(v) for v in rect)
        if kind == "a" and i < len(self.t.regions):
            self.t.regions[i].rect = rect
        elif kind == "q" and i < len(self.t.regions):
            self.t.regions[i].question_rect = rect
            if self.t.regions[i].question_text.strip():
                self.lbl_hint.setText(f"Question area of {self.t.regions[i].problem} changed — "
                                      "use “Extract from image” if its LaTeX should be re-read.")
        elif kind == "n":
            self.t.name_rect = rect
        elif kind == "i":
            self.t.id_rect = rect
        elif kind == "v":
            self.t.version_rect = rect
        else:
            return
        self._mark_dirty()
        self._refresh_overlays()

    # ---------- main question (stem) of sub-questions ----------
    def _show_stem(self):
        """Show the shared main-question box when the selected region is a part ('1b')."""
        part = part_of(self.t.regions[self.sel].problem) if (self.t and self.sel is not None) else None
        self.stem_box.setVisible(part is not None)
        self.lbl_question.setText(f"Part ({part[1]}) (LaTeX)" if part else "Question (LaTeX)")
        if part is None:
            return
        num, letter = part
        sib = siblings(self.t, self.sel)
        names = [self.t.regions[j].problem for j in sib]
        self.lbl_stem.setText(f"Main question {num} (shared by {', '.join(names)})")
        upto = [f"({part_of(self.t.regions[j].problem)[1]})" for j in sib[:sib.index(self.sel) + 1]]
        self.lbl_stem_ctx.setText(f"Grading {self.t.regions[self.sel].problem} sends: main question + part "
                                  + " + part ".join(upto))
        text = self.t.stems.get(num, "")
        if self.in_stem.toPlainText() != text:
            was, self._loading = self._loading, True
            self.in_stem.setPlainText(text)
            self._loading = was

    def _stem_edited(self):
        if self._loading or self.t is None or self.sel is None:
            return
        part = part_of(self.t.regions[self.sel].problem)
        if part is None:
            return
        text = self.in_stem.toPlainText()
        if self.t.stems.get(part[0], "") != text:
            self.t.stems[part[0]] = text
            self._mark_dirty()

    def select_region(self, row):
        if self._loading:
            return
        self.sel = row if self.t and 0 <= row < len(self.t.regions) else None
        self._loading = True
        r = self.t.regions[self.sel] if self.sel is not None else None
        self.in_problem.setText(r.problem if r else "")
        self.in_group.setText(r.group if r else "")
        self.in_points.setValue(r.points if r else 0)
        self.in_question.setPlainText(r.question_text if r else "")
        self.rubric_editor.set_rubric(r.rubric if r else None, r.points if r else 0)
        self.in_notes.setPlainText(r.grading_notes if r else "")
        self._loading = False
        key = r.answer_key.strip() if r else ""
        self.lbl_answer_key.setText(f"Answer key: {key}  (rubrics are written from it)" if key else "")
        self.lbl_answer_key.setVisible(bool(key))
        self._show_stem()
        # the region's answer box becomes the active box, unless one of its own boxes already is
        canvas = self.viewer.canvas
        if r is None:
            if canvas.active_key and canvas.active_key[0] in ("a", "q"):
                canvas.active_key = None
        elif canvas.active_key not in (("a", self.sel), ("q", self.sel)):
            canvas.active_key = ("a", self.sel)
        if r is not None and r.page != self.page:
            self.set_page(r.page)      # also refreshes overlays
        else:
            self._refresh_overlays()
        self._update_enabled()

    def _set_field(self, field, value):
        if self._loading or self.t is None or self.sel is None:
            return
        r = self.t.regions[self.sel]
        if getattr(r, field) == value:
            return
        setattr(r, field, value)
        if field == "points":
            r.rubric.max_points = value
            self.rubric_editor.set_max_points(value)
        item = self.region_list.item(self.sel)
        if item:
            item.setText(self._region_text(r))
        if field in ("problem", "points"):
            self._refresh_overlays()
        if field == "problem":
            self._show_stem()
        if field in ("points", "rubric"):
            self._update_enabled()
        self._mark_dirty()

    def _rubric_edited(self):
        """The rubric editor changed (items, points, order, scoring or expected answer)."""
        if self._loading or self.t is None or self.sel is None:
            return
        r = self.t.regions[self.sel]
        r.rubric = self.rubric_editor.rubric()
        r.rubric.max_points = r.points
        item = self.region_list.item(self.sel)
        if item:
            item.setText(self._region_text(r))
        self._update_enabled()
        self._mark_dirty()

    def delete_region(self):
        if self.t is None or self.sel is None or self.worker is not None:
            return
        row = self.sel
        r = self.t.regions[row]
        if self.t.id is not None:
            n = count_region_grades(self.t.id, row)
            if n and self._grading_running():
                QMessageBox.information(self, "Delete question", "The AI is grading tests of this version. "
                                                                 "Stop it in LLM Grade first.")
                return
            if n and QMessageBox.question(
                    self, "Delete question",
                    f"{n} test{'s have' if n != 1 else ' has'} a grade for {r.problem}. Deleting the question "
                    "deletes those grades too (when the change is saved). Continue?"
            ) != QMessageBox.StandardButton.Yes:
                return
        del self.t.regions[row]
        self._rebuild_region_list()
        self.sel = None
        if self.t.regions:
            self.region_list.setCurrentRow(min(row, len(self.t.regions) - 1))
        else:
            self.select_region(-1)
        self._mark_dirty()
        self._refresh_overlays()

    # ---------- LLM: question extraction and rubric generation ----------
    def run_ai(self, kind, selected_only=False, redo=False):
        """redo (rubrics): regenerate every rubric from scratch, replacing the existing ones."""
        if self.t is None or self.worker is not None:
            return
        if redo:
            todo = [(i, r) for i, r in enumerate(self.t.regions) if r.points > 0]
            if not todo:
                QMessageBox.information(self, "Rubrics", "Set the max points first."); return
            n_old = sum(1 for _, r in todo if r.rubric)
            if n_old and QMessageBox.question(
                    self, "Redo all rubrics",
                    f"Replace {n_old} existing rubric(s) with new ones written from scratch?\n\n"
                    "Edits you made to them are lost.") != QMessageBox.StandardButton.Yes:
                return
            self._start_rubrics(todo)
            return
        if selected_only:
            if self.sel is None:
                return
            r = self.t.regions[self.sel]
            if kind == "extract" and r.question_text.strip() and QMessageBox.question(
                    self, "Re-extract", "Replace the current question text?") != QMessageBox.StandardButton.Yes:
                return
            if kind == "rubric" and r.rubric and QMessageBox.question(
                    self, "Regenerate", "Replace the current rubric?") != QMessageBox.StandardButton.Yes:
                return
            if kind == "rubric" and r.points <= 0:
                QMessageBox.information(self, "Rubric", "Set the max points first."); return
            todo = [(self.sel, r)]
        else:
            has = (lambda r: r.question_text.strip()) if kind == "extract" else (lambda r: bool(r.rubric))
            todo = [(i, r) for i, r in enumerate(self.t.regions) if not has(r)]
            if kind == "rubric":
                skipped = [r.problem for _, r in todo if r.points <= 0]
                todo = [(i, r) for i, r in todo if r.points > 0]
                if skipped:
                    QMessageBox.information(self, "Rubrics", "Skipping regions with 0 max points: " + ", ".join(skipped))
            if not todo:
                self.window().statusBar().showMessage("Nothing to do: all regions already have one.", 4000)
                return
        if kind == "extract":
            snapshot = [(i, copy.copy(r)) for i, r in todo]
            self._start_worker(Worker(job_extract, list(self.t.pages), snapshot, first_parts(self.t)),
                               len(snapshot))
        else:
            self._start_rubrics(todo)

    def _start_rubrics(self, todo):
        snapshot = [(i, copy.copy(r)) for i, r in todo]
        self._save_instructions()
        worker = Worker(job_rubric, list(self.t.pages), snapshot, self.current_instructions(),
                        copy.deepcopy(self.t))
        self.lbl_instr_hint.setVisible(False)
        self._start_worker(worker, len(snapshot))

    def _copy_sources(self) -> List[TestTemplate]:
        """Other versions of this exam that have boxes."""
        if self.t is None:
            return []
        return [v for v in list_versions(self.t.exam_name, self.t.course_id) if v.id != self.t.id and v.regions]

    def copy_boxes_from_version(self):
        """Fill an empty version with the outline of another version (boxes, points, fields)."""
        if self.t is None or self.worker is not None:
            return
        srcs = self._copy_sources()
        if not srcs:
            return
        src = srcs[0]
        if len(srcs) > 1:
            labels = [v.version_label for v in srcs]
            choice, ok = QInputDialog.getItem(self, "Copy boxes", "Copy the boxes of version:", labels, 0, False)
            if not ok:
                return
            src = srcs[labels.index(choice)]
        if self.t.regions and QMessageBox.question(
                self, "Copy boxes", f"Replace the {len(self.t.regions)} region(s) of this version with the "
                                    f"boxes of version {src.version_label}?" + self._grades_lost_note()
        ) != QMessageBox.StandardButton.Yes:
            return
        dup = duplicate_as_version(src, list(self.t.pages), self.t.version_label, keep_text=False)
        self.t.regions = dup.regions
        for attr in ("name_rect", "name_page", "id_rect", "id_page", "version_rect", "version_page", "columns"):
            setattr(self.t, attr, getattr(dup, attr))
        if dup.scan_layout and not self.t.scan_layout:
            self.t.scan_layout = dup.scan_layout
        self.viewer.canvas.active_key = None
        self.sel = None
        self._rebuild_region_list()
        if self.t.regions:
            self.region_list.setCurrentRow(0)
        self._sync_layout_controls()
        self._mark_dirty()
        self._refresh_overlays()
        self._update_enabled()

    # ---------- answer key (JSON written from the exam's .tex) ----------
    def import_answer_key(self, path: str = "", tex: bool = False):
        """Fill the boxes' questions, points, answers and rubrics from an answer key (.json) or
        straight from the exam's LaTeX source (.tex)."""
        if self.t is None or self.worker is not None:
            return
        title = "Import .tex" if tex else "Import answer key"
        if not path:
            filt = ("LaTeX exam (*.tex);;All files (*)" if tex else
                    "LMGrader answer key (*.json);;All files (*)")
            path, _ = QFileDialog.getOpenFileName(self, title, "", filt)
            if not path:
                return
        try:
            key = load_tex(path) if path.lower().endswith(".tex") else load_answer_key(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, title, f"Could not read {Path(path).name}:\n\n{e}")
            return
        if not self.t.regions:
            QMessageBox.information(self, title, "This version has no boxes yet. Detect or draw the answer "
                                                 "boxes first; the key is matched to them by label (1a, 2, ...).")
            return
        rep = apply_answer_key(copy.deepcopy(self.t), key)    # preview with the key's rubrics
        replace = False
        if rep.matched:
            # re-import of an edited key: notes/answers it no longer has would otherwise stay
            rep_r = apply_answer_key(copy.deepcopy(self.t), key, replace=True)
            if rep_r.notes_cleared or rep_r.answers_cleared:
                replace = self._ask_replace(rep_r)
                if replace is None:
                    return
                if replace:
                    rep = rep_r
        if not rep.matched:
            QMessageBox.warning(self, title,
                                "No box label matches the key.\n\nKey: " + ", ".join(it.label for it in key.items)
                                + "\nBoxes: " + ", ".join(r.problem for r in self.t.regions)
                                + "\n\nRename the boxes (Problem field) to the printed numbers.")
            return
        msg = rep.summary()
        if key.warnings:
            msg += "\n\n" + "\n".join(f"Note: {w}" for w in key.warnings)
        if key.exam_version and key.exam_version.strip().upper() != self.t.version_label.strip().upper():
            msg += f"\n\nWarning: the key is for version {key.exam_version}; this is version {self.t.version_label}."
        if rep.replaced:
            msg += "\n\nReplaces the current question text / rubric of: " + ", ".join(rep.replaced)
        cur = self.current_instructions().strip()
        set_instr = bool(key.instructions) and key.instructions != cur and bool(self._instr_exam)
        if set_instr:
            msg += ("\n\nAlso replaces the exam's grading instructions with the key's." if cur else
                    "\n\nAlso sets the exam's grading instructions from the key.")
        use_rubrics = self._confirm_key_import(msg, len(rep.rubrics_from_key))
        if use_rubrics is None:
            return
        trial = copy.deepcopy(self.t)
        apply_answer_key(trial, key, rubrics=use_rubrics, replace=replace)
        self.t.regions, self.t.stems = trial.regions, trial.stems
        if set_instr:
            self.in_instructions.setPlainText(key.instructions)
            self._instr_dirty = True
            self._save_instructions()
        row = self.sel if self.sel is not None else 0
        self.sel = None
        self._rebuild_region_list()
        self.region_list.setCurrentRow(min(row, len(self.t.regions) - 1))
        self._mark_dirty()
        self._refresh_overlays()
        self._update_enabled()
        todo = [r.problem for r in self.t.regions if r.answer_key and not r.rubric]
        graded = sum(1 for s in list_submissions(template_id=self.t.id) if s.status == SUB_GRADED)
        self.window().statusBar().showMessage(
            f"Imported {len(rep.matched)} question(s)." +
            (" Cleared notes/answers not in the key." if replace else "") +
            (f" Rubrics still to write ({', '.join(todo)}): Generate rubrics uses the key's answers." if todo else "") +
            (f" {graded} test(s) of this version were graded before: Regrade them in LLM Grade."
             if graded else ""),
            15000)

    def _ask_replace(self, rep) -> Optional[bool]:
        """This version has notes / expected answers the new key doesn't have. True: clear them
        (replace with the key), False: keep them, None: cancel the import."""
        box = QMessageBox(self)
        box.setWindowTitle("Import answer key")
        lines = []
        if rep.notes_cleared:
            lines.append(f"Notes: {', '.join(rep.notes_cleared)}")
        if rep.answers_cleared:
            lines.append(f"Expected answers: {', '.join(rep.answers_cleared)}")
        box.setText("This version has question notes or answers that the new key doesn't contain:\n\n"
                    + "\n".join(lines) +
                    "\n\nReplace clears them, so the version matches the key exactly. Keep leaves them "
                    "as they are (only what the key contains is imported).")
        b_replace = box.addButton("Replace (clear them)", QMessageBox.ButtonRole.AcceptRole)
        b_keep = box.addButton("Keep them", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(b_replace)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is b_replace else False if clicked is b_keep else None

    def _confirm_key_import(self, summary: str, n_rubrics: int) -> Optional[bool]:
        """Import dialog. None = cancelled; otherwise whether to take the rubrics from the key
        (unchecked: the LLM writes them later from the key's answers)."""
        box = QMessageBox(self)
        box.setWindowTitle("Import answer key")
        box.setText(summary + "\n\nImport?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        cb = None
        if n_rubrics:
            cb = QCheckBox(f"Import the rubrics from the key ({n_rubrics} question(s))")
            cb.setToolTip("Unchecked: only questions, points and answers are imported; "
                          "Generate rubrics then writes the rubrics from the key's answers.")
            cb.setChecked(True)
            box.setCheckBox(cb)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return None
        return bool(cb and cb.isChecked())

    def answer_key_help(self):
        box = QMessageBox(self)
        box.setWindowTitle("Answer keys from LaTeX")
        target = Path.home() / ".claude" / "skills" / "lmgrader-answer-key" / "SKILL.md"
        box.setText(
            "Skip reading the printed exam with the LLM: import the questions straight from your .tex.\n\n"
            "1. Install the Claude Code skill (once).\n"
            "2. In Claude Code, in your exam's folder: \"write the LMGrader answer key for midterm.tex\".\n"
            "   It writes midterm.lmgrader.json: exact LaTeX, points, answers (and rubrics if you ask).\n"
            "3. Here: detect or draw the boxes, then Import answer key….\n\n"
            f"The skill is installed to:\n{target}")
        b_install = box.addButton("Install Claude Code skill", QMessageBox.ButtonRole.AcceptRole)
        b_copy = box.addButton("Copy instructions", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        text = SKILL_PATH.read_text(encoding="utf-8")
        if box.clickedButton() is b_install:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Install skill", f"Could not write {target}:\n{e}")
                return
            QMessageBox.information(self, "Install skill", f"Installed. Claude Code uses it in any folder:\n{target}")
        elif box.clickedButton() is b_copy:
            QApplication.clipboard().setText(text)
            self.window().statusBar().showMessage("Instructions copied: paste them to Claude with your .tex.", 6000)

    def _start_worker(self, worker, steps, on_done=None):
        self.flush()
        self._ai_tid = self.t.id
        self.worker = worker
        self._finished_workers = [w for w in getattr(self, "_finished_workers", []) if w.isRunning()]
        worker.progress.connect(self._ai_progress)
        worker.item.connect(self._ai_item)
        worker.done.connect(on_done or (lambda _: self._ai_finished(None)))
        worker.failed.connect(self._ai_finished)
        self.ai_progress.setVisible(True)
        self.ai_progress.setRange(0, max(1, steps)); self.ai_progress.setValue(0)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("Stop")
        worker.start()
        self._update_enabled()

    def stop_ai(self):
        """Stop button: abort the running LLM job; finished items stay applied (and autosaved)."""
        if self.worker is None:
            return
        self.worker.stop()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("Stopping…")
        self.ai_status.setText("Stopping…")

    def _ai_progress(self, i, total, msg):
        self.ai_progress.setRange(0, max(1, total))
        self.ai_progress.setValue(i)
        self.ai_status.setText(msg)

    def _ai_item(self, payload):
        if self.t is None or self.t.id != self._ai_tid:
            return
        if payload[0] == "regions":
            self._apply_regions(*payload[1:])
            return
        kind, i, text = payload
        if kind == "stem":     # i is the main question number; never overwrite an existing stem
            if not self.t.stems.get(i, "").strip():
                self.t.stems[i] = text
                self._show_stem()
                self._mark_dirty()
            return
        if i >= len(self.t.regions):
            return
        r = self.t.regions[i]
        if kind == "question":
            r.question_text = text
        else:
            r.set_rubric(text)       # a Rubric from the LLM (or text from a custom client)
        self.region_list.item(i).setText(self._region_text(r))
        if i == self.sel:
            self.select_region(i)
        self._mark_dirty()

    def _ai_finished(self, error):
        w = self.worker
        stopped = w is not None and w.cancelled
        if w is not None:
            self._finished_workers = getattr(self, "_finished_workers", []) + [w]   # keep alive until it exits
        self.worker = None
        self.ai_progress.setVisible(False)
        self.btn_stop.setVisible(False)
        if stopped:                       # errors after Stop are just the aborted request
            self.ai_status.setText(STOPPED_MSG)
        elif error:
            self.ai_status.setText("")
            QMessageBox.critical(self, "LLM error", f"{error}\n\nCheck that LM Studio is running (Settings).")
        else:
            step = self.current_step()
            self.ai_status.setText({1: "Done — check the boxes and set the points, then generate rubrics.",
                                    3: "Done — review the rubrics; they are now fixed for grading."}.get(
                step, "Done — review the text, it is now fixed for grading."))
        self.flush()
        self._update_enabled()

    # ---------- LLM: automatic region detection (all pages) ----------
    def run_detect(self, autosetup=False):
        if self.t is None or self.worker is not None:
            return
        replace = True
        if self.t.regions:
            box = QMessageBox(QMessageBox.Icon.Question, "Auto-detect regions",
                              f"This version already has {len(self.t.regions)} region(s). "
                              "What should happen to them?" + self._grades_lost_note("Replace all"), parent=self)
            b_replace = box.addButton("Replace all", QMessageBox.ButtonRole.DestructiveRole)
            b_add = box.addButton("Add missing only", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(b_add)
            box.exec()
            if box.clickedButton() not in (b_replace, b_add):
                return
            replace = box.clickedButton() is b_replace
        self.start_detect(autosetup, replace)

    def start_detect(self, autosetup: bool, replace: bool):
        """Phase 1 (regions on all pages). With autosetup, a confirm banner then offers phase 2
        (LaTeX) — nothing is transcribed until the user has checked the boxes and continues."""
        existing = [copy.copy(r) for r in self.t.regions]
        pages = list(self.t.pages)
        self.dismiss_latex_banner()
        self._start_worker(Worker(job_layout, pages, existing, replace, int(self.t.columns or 0)), len(pages),
                           on_done=lambda result: self._detect_finished(result, autosetup))
        self.ai_status.setText("Finding questions… (about a minute per page)")

    def _detect_finished(self, result, autosetup=False):
        tid_ok = self.t is not None and self.t.id == self._ai_tid
        stopped = self.worker is not None and self.worker.cancelled
        if tid_ok and result is not None:
            self._apply_regions(*result)
        self._ai_finished(None)
        if tid_ok and autosetup and not stopped and self.t.regions:
            self._latex_pending_tid = self.t.id
            self.confirm_banner.setVisible(True)
            self.ai_status.setText("Step 1 of auto-setup done — check the boxes, then continue.")

    # ---------- phase 2 of auto-setup: LaTeX after the user confirmed the boxes ----------
    def continue_latex(self):
        if self.t is None or self.worker is not None:
            return
        self.dismiss_latex_banner()
        todo = [(i, copy.copy(r)) for i, r in enumerate(self.t.regions) if not r.question_text.strip()]
        if not todo:
            self.ai_status.setText("Every region already has its question.")
            return
        self._start_worker(Worker(job_extract, list(self.t.pages), todo, first_parts(self.t)), len(todo))

    def dismiss_latex_banner(self):
        self._latex_pending_tid = None
        self.confirm_banner.setVisible(False)

    def latex_pending(self) -> bool:
        return self.t is not None and self._latex_pending_tid == self.t.id

    def _apply_regions(self, regions, name_rect, name_page, added, id_rect=None, id_page=0):
        # replace mode: every region is new, so the detected name/ID fields replace the old ones too;
        # add mode: only fill a field if none is set yet
        replaced = added == len(regions)
        self.t.regions = list(regions)
        if name_rect and (self.t.name_rect is None or replaced):
            self.t.name_rect, self.t.name_page = name_rect, name_page
        if id_rect and (self.t.id_rect is None or replaced):
            self.t.id_rect, self.t.id_page = id_rect, id_page
        self._rebuild_region_list()
        self.sel = None
        if self.t.regions:
            self.region_list.setCurrentRow(0)
        else:
            self.select_region(-1)
        self._mark_dirty()
        self._refresh_overlays()
        self.window().statusBar().showMessage(
            f"Auto-detect added {added} region(s). Check the boxes and set the points.", 6000)

    def apply_layout(self, layout, replace=True) -> int:
        """Apply a single-page detect_layout() result to the current page. Returns regions added."""
        result = regions_from_layouts([(self.page, layout)], self.t.regions, replace)
        self._apply_regions(*result)
        return result[3]

    def get_templates(self):
        return list_templates()
