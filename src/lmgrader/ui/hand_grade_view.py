"""Hand Grade: grade one question across every test of an exam version, by groups of answers.

0. Category (asked the first time a question is opened, group_setup.ask_kind): blank answers
   only, short answer (identical TrOCR readings grouped) or multiple choice (the choice boxes
   are checked once, group_setup.ChoiceEditor; tests grouped by the marked choice). Suggested
   groups are flagged ⚑ until the human presses Looks right (or grades the group). Change
   question type… picks another category and starts the question over (asked first); Regrade
   from scratch… starts it over with the same category.
1. Blanks: the blank-answer CNN proposes the empty answer boxes; click the ones that are not
   blank; the rest join the "Blank" group (every question has one, so missed blanks can be
   added to it in step 2).
2. Group: click answers that are the same (a click toggles one, as Ctrl-click does; or drag a
   box) and make them a group or add them to one (Add selection to group). Dissolving a group
   deletes its answers' hand grades (they are graded again).
3. Confirm groups: every group with all its answers on one page; select the answers that don't
   belong and move them to ungrouped. Groups are right marks the suggested groups as checked.
4. Grade: every group once (its rubric items + comment apply to all its answers), then every
   ungrouped answer on its own. Check rubric items (keys 1-9), add a comment, Ctrl+Enter saves
   and moves on; when the question is fully graded it opens the next question.
Hand grades are final: the LLM never grades these answers again. Everything is saved at once.
"""
from html import escape
from typing import Dict, List, Optional

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QListWidget, QListWidgetItem,
                             QStackedWidget, QButtonGroup, QSplitter, QPlainTextEdit, QCheckBox, QLabel,
                             QScrollArea, QFrame, QMessageBox, QInputDialog, QProgressBar, QSlider, QDialog,
                             QDialogButtonBox, QAbstractItemView, QSizePolicy, QMenu)
from PyQt6.QtCore import Qt, QSize, QEvent
from PyQt6.QtGui import QPixmap, QIcon, QColor, QKeySequence, QShortcut, QFontInfo

from ..db import (list_exams, list_versions, list_students, list_submissions_by_status, get_hand_stage,
                  set_hand_stage, add_hand_group, set_hand_group_members, remove_from_hand_groups,
                  update_hand_group, delete_grade, load_template, region_grades,
                  get_hand_setup, set_hand_setup)
from ..grading import hand, autogroup, marks
from ..grading.session import refresh_status
from ..template.alignment import read_image
from ..template.template import persist
from ..models import fmt_points, SUB_NEW
from . import theme
from .theme import button, card, label, pill, setup_page
from .widgets import ImageThumb, PageViewer, Overlay, Worker
from .mathtext import latex_html
from .rubric_editor import RubricEditor
from . import group_setup

PAGE_EMPTY, PAGE_BLANKS, PAGE_GROUP, PAGE_CONFIRM, PAGE_GRADE = range(5)
STAGES = ("blanks", "group", "confirm", "grade")        # stage button n = STAGES[n - 1] = page n
STAGE_PAGE = {s: n for n, s in enumerate(STAGES, start=1)}
ROLE_SUB = Qt.ItemDataRole.UserRole
ROLE_ENTRY = Qt.ItemDataRole.UserRole + 1

MODE_PLAIN, MODE_BLANKS = "plain", "blanks"
SELECT_HINT = ("Click the answers that should get the same grade (click again to unselect, or drag a box "
               "around several), then make them a group, or click a group in the list to add them to it.")

def job_prepare(template, i, mode, choices=None, worker=None):
    """Cut out the answers; mode blanks / short: also run the blank CNN; short / choice: also
    suggest groups (autogroup). Returns (answers, autogroup.Summary or None)."""
    prog = lambda a, b, m: worker.progress.emit(a, b, m)
    stop = lambda: worker.cancelled
    answers = hand.prepare_answers(template, i, mode in (MODE_BLANKS, autogroup.KIND_SHORT),
                                   progress=prog, should_stop=stop)
    summary = None
    if mode in (autogroup.KIND_SHORT, autogroup.KIND_CHOICE) and not worker.cancelled:
        summary = autogroup.suggest_groups(template, i, mode, answers, choices, prog, stop)
    return answers, summary

def _to_check(g: dict) -> bool:
    """A suggested group (⚑) the human hasn't checked yet; an empty group has nothing to check."""
    return not g["checked"] and bool(g["members"])

class AnswerGrid(QListWidget):
    """Thumbnails of answer crops in a grid; several can be selected. toggle: a click adds or
    removes one answer from the selection, as Ctrl-click does elsewhere."""
    def __init__(self, selectable=True, toggle=False):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(8)
        self.setWordWrap(True)
        mode = QAbstractItemView.SelectionMode
        self.setSelectionMode(mode.NoSelection if not selectable else
                              mode.MultiSelection if toggle else mode.ExtendedSelection)
        self.setSelectionRectVisible(selectable)      # drag a box around answers to select them
        self._pix: Dict[str, QPixmap] = {}
        self._aspect = 0.45            # thumbnail height / width: the answer box's own shape
        self.set_thumb_width(280)

    def set_aspect(self, rect):
        """Shape the thumbnails like the question's answer box (x, y, w, h)."""
        w, h = (rect[2], rect[3]) if rect else (1, 0.45)
        self._aspect = max(0.25, min(1.3, h / max(1, w)))
        self.set_thumb_width(self._w)

    def set_thumb_width(self, w: int):
        self._w = w
        h = int(w * self._aspect)
        self.setIconSize(QSize(w, h))
        self.setGridSize(QSize(w + 18, h + 46))
        for r in range(self.count()):
            it = self.item(r)
            it.setIcon(self._icon(it.data(Qt.ItemDataRole.UserRole + 2)))

    def _icon(self, path) -> QIcon:
        if not path:
            return QIcon()
        if path not in self._pix:
            self._pix[path] = QPixmap(path)
        pm = self._pix[path]
        if pm.isNull():
            return QIcon()
        return QIcon(pm.scaled(self.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation))

    def add_answer(self, sub_id: int, crop: str, caption: str, tooltip: str = "") -> QListWidgetItem:
        it = QListWidgetItem(self._icon(crop), caption if crop else f"{caption}\n(page missing)")
        it.setData(ROLE_SUB, sub_id)
        it.setData(Qt.ItemDataRole.UserRole + 2, crop)
        it.setToolTip(tooltip)
        it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.addItem(it)
        return it

    def selected_ids(self) -> List[int]:
        return [it.data(ROLE_SUB) for it in self.selectedItems()]

class FitGrid(AnswerGrid):
    """An AnswerGrid as tall as its answers, without a scroll bar of its own: Confirm groups
    stacks one per group and the page scrolls. pix: a thumbnail cache shared by the grids."""
    def __init__(self, pix: Dict[str, QPixmap] = None):
        super().__init__(toggle=True)
        if pix is not None:
            self._pix = pix
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.fit()

    def fit(self):
        """Height for every row of answers at the current width (laid out now, not later)."""
        self.doItemsLayout()
        h = self.gridSize().height()
        if self.count():         # the last row's top + one grid cell (rows are one cell tall)
            h += self.visualItemRect(self.item(self.count() - 1)).top() + self.verticalScrollBar().value()
        self.setFixedHeight(h + 2 * self.frameWidth() + 4)

    def set_thumb_width(self, w: int):
        super().set_thumb_width(w)
        self.fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.size().width() != e.oldSize().width():
            self.fit()

    def wheelEvent(self, e):
        e.ignore()               # nothing to scroll in here: the page behind scrolls

RUBRIC_PT = 12        # text size of the rubric while grading (the rest of the app is 10pt)

def _rubric_font(w: QWidget, pt: int = RUBRIC_PT) -> QWidget:
    """Grading-size text on `w` (its own style sheet beats the app-wide 10pt), applied now so
    formulas drawn for it get the same size."""
    w.setStyleSheet(f"font-size: {pt}pt;")
    w.ensurePolished()
    return w

class ItemRow(QWidget):
    """One rubric item in the grading panel: checkbox + key number + points + description."""
    def __init__(self, n: int, item, on_toggle):
        super().__init__()
        lay = QHBoxLayout(self); lay.setContentsMargins(4, 5, 4, 5); lay.setSpacing(10)
        self.item = item
        self.chk = QCheckBox()
        self.chk.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")
        self.chk.toggled.connect(lambda _: on_toggle())
        lay.addWidget(self.chk, 0, Qt.AlignmentFlag.AlignTop)
        key = _rubric_font(label(str(n) if n <= 9 else "", "muted"), RUBRIC_PT - 1)
        key.setFixedWidth(key.fontMetrics().horizontalAdvance("9") + 6)
        lay.addWidget(key, 0, Qt.AlignmentFlag.AlignTop)
        color = theme.OK if item.points > 0 else theme.DANGER if item.points < 0 else theme.MUTED
        pts = _rubric_font(QLabel(f"<b style='color:{color}'>{fmt_points(item.points)}</b>"))
        pts.setFixedWidth(pts.fontMetrics().horizontalAdvance("−10.5") + 8)
        lay.addWidget(pts, 0, Qt.AlignmentFlag.AlignTop)
        self.desc = _rubric_font(QLabel())
        self.desc.setTextFormat(Qt.TextFormat.RichText)
        self.desc.setWordWrap(True)
        self.desc.setText(latex_html(item.description or "(no description)", theme.TEXT,
                                     QFontInfo(self.desc.font()).pixelSize()))
        lay.addWidget(self.desc, 1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Key {n}" if n <= 9 else "")

    def mousePressEvent(self, e):
        self.chk.toggle()

class HandGradeView(QWidget):
    def __init__(self, template_view=None):
        super().__init__()
        self.template_view = template_view
        body, actions = setup_page(self, "Hand Grade",
                                   "One question at a time across every test: LMGrader groups blank, identical "
                                   "or same-choice answers, you check the groups, then check rubric items. The "
                                   "LLM never re-grades your grades.")
        self.btn_change_kind = button("Change question type…", tooltip="Blank answers only, short answer or "
                                                                       "multiple choice: grading this question "
                                                                       "starts over")
        self.btn_regrade = button("Regrade from scratch…", tooltip="Delete this question's groups and hand grades "
                                                                   "and group and grade it again (same question "
                                                                   "type)")
        self.btn_edit_rubric = button("Edit rubric…", tooltip="Add, change or reorder this question's rubric items "
                                                              "(saved to the exam, also in Exams & Templates)")
        actions.addWidget(self.btn_regrade)
        actions.addWidget(self.btn_change_kind)
        actions.addWidget(self.btn_edit_rubric)
        self.t = None                    # template (version) being graded
        self.qi: Optional[int] = None    # region index
        self.answers: Dict[int, hand.Answer] = {}
        self.order: List[int] = []       # submission ids in test order
        self.nums: Dict[int, int] = {}
        self.names: Dict[int, str] = {}
        self.worker: Optional[Worker] = None
        self.queue: List[tuple] = []
        self.cur: Optional[tuple] = None
        self.item_rows: List[ItemRow] = []
        self._loading = False

        # ---- question picker ----
        bar_card, bar = card(QHBoxLayout, margins=10)
        bar.addWidget(label("Exam"))
        self.cb_exam = QComboBox(); self.cb_exam.setMinimumWidth(180)
        bar.addWidget(self.cb_exam)
        bar.addSpacing(8)
        bar.addWidget(label("Version"))
        self.cb_version = QComboBox(); self.cb_version.setMinimumWidth(90)
        bar.addWidget(self.cb_version)
        bar.addSpacing(8)
        bar.addWidget(label("Question"))
        self.cb_question = QComboBox(); self.cb_question.setMinimumWidth(260)
        bar.addWidget(self.cb_question, 1)
        bar.addSpacing(8)
        self.lbl_counts = label("", "muted")
        bar.addWidget(self.lbl_counts)
        body.addWidget(bar_card)

        # ---- stages ----
        srow = QHBoxLayout()
        self.stage_group = QButtonGroup(self)
        self.stage_btns = []
        self.stage_arrows = []      # hidden with the buttons while no question is open
        for i, text in enumerate(("1  Blanks", "2  Group answers", "3  Confirm groups", "4  Grade")):
            b = button(text)
            b.setCheckable(True); b.setMinimumHeight(36)
            self.stage_group.addButton(b, i + 1)
            self.stage_btns.append(b)
            srow.addWidget(b, 1)
            if i < 3:
                arrow = label("→", "muted")
                self.stage_arrows.append(arrow)
                srow.addWidget(arrow)
        body.addLayout(srow)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_empty())
        self.stack.addWidget(self._build_blanks())
        self.stack.addWidget(self._build_group())
        self.stack.addWidget(self._build_confirm())
        self.stack.addWidget(self._build_grade())
        body.addWidget(self.stack, 1)

        prow = QHBoxLayout()
        self.lbl_progress = label("", "muted")
        self.progress = QProgressBar(); self.progress.setVisible(False)
        self.btn_stop = button("Stop", "danger"); self.btn_stop.setVisible(False)
        prow.addWidget(self.lbl_progress); prow.addWidget(self.progress, 1); prow.addWidget(self.btn_stop)
        body.addLayout(prow)

        self.cb_exam.currentIndexChanged.connect(lambda _: self._load_versions())
        self.cb_version.currentIndexChanged.connect(lambda _: self._load_questions())
        self.cb_question.activated.connect(lambda _: self.open_question())
        self.stage_group.idClicked.connect(self._stage_clicked)
        self.btn_edit_rubric.clicked.connect(self.edit_rubric)
        self.btn_change_kind.clicked.connect(self.change_kind)
        self.btn_regrade.clicked.connect(self.regrade_from_scratch)
        self.btn_stop.clicked.connect(lambda: self.worker and self.worker.stop())
        self._show_page(PAGE_EMPTY)
        self.refresh()

    # ================= pages =================
    def _build_empty(self):
        w, lay = card(margins=24)
        self.lbl_empty = label("Pick a question above to grade it by hand.", "muted")
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(); lay.addWidget(self.lbl_empty); lay.addStretch()
        return w

    def _build_blanks(self):
        w, lay = card(margins=12)
        self.lbl_blank_info = label("")
        self.lbl_blank_info.setWordWrap(True)
        lay.addWidget(self.lbl_blank_info)
        self.grid_blanks = AnswerGrid(selectable=False)
        lay.addWidget(self.grid_blanks, 1)
        row = QHBoxLayout()
        self.lbl_blank_count = label("", "muted")
        row.addWidget(self.lbl_blank_count); row.addStretch()
        self.btn_blank_skip = button("Don't sort blanks", "ghost", "Treat every answer normally")
        self.btn_blank_ok = button("Confirm blanks  →", "primary")
        row.addWidget(self.btn_blank_skip); row.addWidget(self.btn_blank_ok)
        lay.addLayout(row)
        self.grid_blanks.itemClicked.connect(self._toggle_blank)
        self.btn_blank_ok.clicked.connect(self.confirm_blanks)
        self.btn_blank_skip.clicked.connect(lambda: self._set_stage("group"))
        return w

    def _build_group(self):
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left, ll = card(margins=12)
        top = QHBoxLayout()
        top.addWidget(label("Show"))
        self.cb_filter = QComboBox(); self.cb_filter.setMinimumWidth(200)
        top.addWidget(self.cb_filter)
        self.btn_select_none = button("Select none", "ghost", "Unselect every answer (Esc)")
        top.addWidget(self.btn_select_none)
        top.addStretch()
        top.addWidget(label("Size", "muted"))
        self.sl_size = QSlider(Qt.Orientation.Horizontal); self.sl_size.setRange(160, 520); self.sl_size.setValue(280)
        self.sl_size.setFixedWidth(120)
        top.addWidget(self.sl_size)
        ll.addLayout(top)
        self.grid_group = AnswerGrid(toggle=True)
        ll.addWidget(self.grid_group, 1)
        self.lbl_group_hint = label(SELECT_HINT, "muted")
        self.lbl_group_hint.setWordWrap(True)
        ll.addWidget(self.lbl_group_hint)
        split.addWidget(left)

        right, rl = card(margins=12)
        rl.addWidget(label("Groups", "sectionTitle"))
        self.btn_suggest = button("Suggest groups…", tooltip="Blank answers only, short answer or multiple "
                                                             "choice: LMGrader groups the answers that are not "
                                                             "grouped or graded yet")
        rl.addWidget(self.btn_suggest)
        self.list_groups = QListWidget()
        self.list_groups.setToolTip("Click a group to see its answers; with answers selected, a click adds "
                                    "them to the group")
        self.list_groups.viewport().installEventFilter(self)    # a click with answers selected adds them
        rl.addWidget(self.list_groups, 1)
        self.btn_check_group = button("Looks right  ✓", "primary", "The answers of this suggested group belong "
                                                                  "together")
        rl.addWidget(self.btn_check_group)
        self.btn_new_group = button("New group from selection", "primary")
        self.btn_add_to_group = button("Add selection to group", tooltip="Add the selected answers to a group, "
                                                                            "e.g. Blank")
        self.menu_add = QMenu(self.btn_add_to_group)
        self.menu_add.aboutToShow.connect(self._fill_add_menu)
        self.btn_add_to_group.setMenu(self.menu_add)
        self.btn_ungroup_sel = button("Take selection out of groups")
        self.btn_rename_group = button("Rename group…", "ghost")
        self.btn_delete_group = button("Dissolve group", "danger", "Its answers become ungrouped again and lose "
                                                                   "their hand grades")
        for b in (self.btn_new_group, self.btn_add_to_group, self.btn_ungroup_sel, self.btn_rename_group,
                  self.btn_delete_group):
            rl.addWidget(b)
        rl.addSpacing(8)
        self.btn_to_confirm = button("Next: confirm groups  →", "primary", "See every group with all its answers "
                                                                          "before grading")
        rl.addWidget(self.btn_to_confirm)
        right.setMinimumWidth(250)
        split.addWidget(right)
        split.setSizes([900, 280])

        self.cb_filter.currentIndexChanged.connect(lambda _: self._render_group_grid())
        self.sl_size.valueChanged.connect(self._resize_thumbs)
        self.btn_select_none.clicked.connect(self.grid_group.clearSelection)
        QShortcut(QKeySequence("Escape"), self.grid_group, activated=self.grid_group.clearSelection,
                  context=Qt.ShortcutContext.WidgetShortcut)
        self.grid_group.itemSelectionChanged.connect(self._update_group_buttons)
        self.list_groups.currentRowChanged.connect(self._group_picked)
        self.list_groups.itemSelectionChanged.connect(self._update_group_buttons)
        self.btn_suggest.clicked.connect(self.suggest_groups)
        self.btn_check_group.clicked.connect(self.check_group)
        self.btn_new_group.clicked.connect(self.new_group)
        self.btn_ungroup_sel.clicked.connect(self.ungroup_selection)
        self.btn_rename_group.clicked.connect(self.rename_group)
        self.btn_delete_group.clicked.connect(self.dissolve_group)
        self.btn_to_confirm.clicked.connect(lambda: self._set_stage("confirm"))
        return split

    def _build_confirm(self):
        """3. Confirm groups: one section per group (its name, then all its answers); answers
        selected in any group can be moved to ungrouped."""
        w, lay = card(margins=12)
        top = QHBoxLayout()
        self.lbl_confirm_info = label("")
        self.lbl_confirm_info.setWordWrap(True)
        top.addWidget(self.lbl_confirm_info, 1)
        top.addWidget(label("Size", "muted"))
        self.sl_confirm_size = QSlider(Qt.Orientation.Horizontal)
        self.sl_confirm_size.setRange(160, 520); self.sl_confirm_size.setValue(self.sl_size.value())
        self.sl_confirm_size.setFixedWidth(120)
        top.addWidget(self.sl_confirm_size)
        lay.addLayout(top)
        self.confirm_scroll = QScrollArea(); self.confirm_scroll.setWidgetResizable(True)
        self.confirm_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.confirm_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box = QWidget()
        self.confirm_layout = QVBoxLayout(box)
        self.confirm_layout.setContentsMargins(0, 0, 0, 0); self.confirm_layout.setSpacing(6)
        self.confirm_scroll.setWidget(box)
        lay.addWidget(self.confirm_scroll, 1)
        self.confirm_grids: List[FitGrid] = []
        self._confirm_pix: Dict[str, QPixmap] = {}
        row = QHBoxLayout()
        self.btn_confirm_none = button("Select none", "ghost", "Unselect every answer (Esc)")
        self.btn_confirm_ungroup = button("Move selected to ungrouped", tooltip="The selected answers leave their "
                                                                                "group and are graded one by one")
        self.btn_confirm_ok = button("Groups are right  →  grade", "primary", "Every group is checked: grade them")
        row.addWidget(self.btn_confirm_none); row.addStretch()
        row.addWidget(self.btn_confirm_ungroup); row.addWidget(self.btn_confirm_ok)
        lay.addLayout(row)
        self.sl_confirm_size.valueChanged.connect(self._resize_thumbs)
        self.btn_confirm_none.clicked.connect(self._confirm_select_none)
        QShortcut(QKeySequence("Escape"), w, activated=self._confirm_select_none,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.btn_confirm_ungroup.clicked.connect(self.confirm_ungroup)
        self.btn_confirm_ok.clicked.connect(self.confirm_groups)
        return w

    def _build_grade(self):
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        # queue
        qcard, ql = card(margins=10)
        ql.addWidget(label("To grade", "sectionTitle"))
        self.list_queue = QListWidget()
        ql.addWidget(self.list_queue, 1)
        self.lbl_queue = label("", "muted")
        ql.addWidget(self.lbl_queue)
        qcard.setMinimumWidth(200)
        split.addWidget(qcard)
        # answers
        acard, al = card(margins=10)
        hrow = QHBoxLayout()
        self.lbl_answer_title = label("", "sectionTitle")
        hrow.addWidget(self.lbl_answer_title); hrow.addStretch()
        self.btn_full_page = button("Full page", "ghost", "The whole scanned page with this answer box outlined")
        hrow.addWidget(self.btn_full_page)
        al.addLayout(hrow)
        self.answer_stack = QStackedWidget()
        self.single_scroll = QScrollArea(); self.single_scroll.setWidgetResizable(True)
        self.single_img = ImageThumb(max_height=900)
        self.single_scroll.setWidget(self.single_img)
        self.answer_stack.addWidget(self.single_scroll)
        self.grid_members = AnswerGrid(selectable=False)
        self.grid_members.set_thumb_width(240)
        self.answer_stack.addWidget(self.grid_members)
        al.addWidget(self.answer_stack, 1)
        self.lbl_ai = label("", "muted")
        self.lbl_ai.setWordWrap(True)
        al.addWidget(self.lbl_ai)
        split.addWidget(acard)
        # rubric panel
        rcard, rl = card(margins=12)
        top = QHBoxLayout()
        top.addWidget(label("Rubric", "sectionTitle")); top.addStretch()
        self.lbl_score = label("", "pageTitle")
        top.addWidget(self.lbl_score)
        rl.addLayout(top)
        self.lbl_scoring = label("", "muted")
        self.lbl_scoring.setWordWrap(True)
        rl.addWidget(self.lbl_scoring)
        self.lbl_expected = _rubric_font(QLabel())
        self.lbl_expected.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_expected.setWordWrap(True)
        rl.addWidget(self.lbl_expected)
        items_box = QWidget()
        self.items_layout = QVBoxLayout(items_box)
        self.items_layout.setContentsMargins(0, 0, 0, 0); self.items_layout.setSpacing(4)
        iscroll = QScrollArea(); iscroll.setWidgetResizable(True); iscroll.setWidget(items_box)
        iscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # descriptions wrap
        iscroll.setMinimumHeight(140)
        rl.addWidget(iscroll, 1)
        self.lbl_no_items = label("This question has no rubric items yet: use Edit rubric… to add them.", "muted")
        self.lbl_no_items.setWordWrap(True)
        rl.addWidget(self.lbl_no_items)
        rl.addWidget(label("Comment (optional, shown to the student)", "muted"))
        self.in_comment = QPlainTextEdit()
        self.in_comment.setMaximumHeight(90)
        self.in_comment.setPlaceholderText("e.g. Good start; check the sign in the last step.")
        rl.addWidget(self.in_comment)
        brow = QHBoxLayout()
        self.btn_prev = button("←", tooltip="Previous (Alt+Left)"); self.btn_prev.setFixedWidth(44)
        self.btn_next = button("→", tooltip="Next without saving (Alt+Right)"); self.btn_next.setFixedWidth(44)
        self.btn_clear = button("Clear", "danger", "Delete the hand grade of this answer / group "
                                                   "(it can then be graded again, also by the LLM)")
        self.btn_save = button("Save && next", "primary", "Save this grade and go to the next ungraded one "
                                                         "(Ctrl+Enter)")
        brow.addWidget(self.btn_prev); brow.addWidget(self.btn_next); brow.addWidget(self.btn_clear)
        brow.addStretch(); brow.addWidget(self.btn_save)
        rl.addLayout(brow)
        self.lbl_applies = label("", "muted")
        self.lbl_applies.setWordWrap(True)
        rl.addWidget(self.lbl_applies)
        rcard.setMinimumWidth(380)
        split.addWidget(rcard)
        split.setSizes([180, 560, 500])     # the rubric gets the room: it is read for every answer

        self.list_queue.currentRowChanged.connect(self._queue_picked)
        self.btn_save.clicked.connect(self.save_current)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_clear.clicked.connect(self.clear_current)
        self.btn_full_page.clicked.connect(self.show_full_page)
        self.in_comment.textChanged.connect(self._update_score)
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(seq), split, activated=self.save_current,
                      context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Alt+Left"), split, activated=lambda: self._step(-1),
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Alt+Right"), split, activated=lambda: self._step(1),
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        for n in range(1, 10):
            QShortcut(QKeySequence(str(n)), split, activated=lambda n=n: self.toggle_item(n - 1),
                      context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        return split

    # ================= picker =================
    def context(self):
        """(exam, version id) shown here; the main window keeps it the same across tabs."""
        return self.cb_exam.currentText() or None, self.cb_version.currentData()

    def set_context(self, exam, tid=None):
        """Show this exam/version at the next refresh (not while answers are being prepared)."""
        if exam and self.worker is None:
            self._want = (exam, tid)

    def refresh(self):
        """Reload exams/versions/questions (keeps the selection) and the open question's data."""
        if self.template_view is not None and hasattr(self.template_view, "flush"):
            self.template_view.flush()          # the editor autosaves; use its latest rubric/boxes
        want, self._want = getattr(self, "_want", None), None
        self.names = {s.id: s.name for s in list_students(all_courses=True)}
        exams = list_exams()
        cur = want[0] if want and want[0] in exams else self.cb_exam.currentText()
        self.cb_exam.blockSignals(True)
        self.cb_exam.clear()
        self.cb_exam.addItems(exams)
        if cur and self.cb_exam.findText(cur) >= 0:
            self.cb_exam.setCurrentIndex(self.cb_exam.findText(cur))
        self.cb_exam.blockSignals(False)
        self._load_versions(reopen=True, want_tid=want[1] if want and want[0] == cur else None)

    def _load_versions(self, reopen=False, want_tid=None):
        cur = self.cb_version.currentData()
        self.cb_version.blockSignals(True)
        self.cb_version.clear()
        for v in (list_versions(self.cb_exam.currentText()) if self.cb_exam.currentText() else []):
            self.cb_version.addItem(f"v{v.version_label}", v.id)
        if want_tid is not None and self.cb_version.findData(want_tid) >= 0:
            cur = want_tid
        self.cb_version.setCurrentIndex(max(0, self.cb_version.findData(cur)))
        self.cb_version.blockSignals(False)
        self._load_questions(reopen=reopen)

    def _load_questions(self, reopen=False):
        tid = self.cb_version.currentData()
        old = self.t
        self.t = load_template(tid) if tid else None
        exam = self.cb_exam.currentText()
        subs = list_submissions_by_status(exam) if exam else []
        self.nums = {s.id: n for n, s in enumerate(subs, start=1)}
        # the open question stays open only within the same version (found by uid: it may have moved)
        same = old is not None and self.t is not None and old.id == self.t.id
        cur_uid = old.regions[self.qi].uid if (same and self.qi is not None
                                               and self.qi < len(old.regions)) else None
        self.cb_question.blockSignals(True)
        self.cb_question.clear()
        self.cb_question.addItem("— choose a question —", None)
        total = len(hand.eligible_submissions(self.t)) if self.t else 0
        for i, r in enumerate(self.t.regions if self.t else []):
            done = len(hand.hand_graded(self.t, i))
            mark = "✓ " if total and done >= total else ""
            self.cb_question.addItem(f"{mark}{r.problem}  ·  {r.points:g} pts  ·  {done}/{total} by hand", r.uid)
        idx = self.cb_question.findData(cur_uid) if cur_uid else -1
        self.cb_question.setCurrentIndex(max(0, idx))
        self.cb_question.blockSignals(False)
        if reopen and idx > 0:
            self.open_question(ask=False)
        elif idx <= 0:
            self.qi = None
            self._show_page(PAGE_EMPTY)
            self.lbl_empty.setText("Pick a question above to grade it by hand." if self.t else
                                   "Create an exam in Exams & Templates and upload scans in Scan & Names first.")
            self._update_header()

    @property
    def region(self):
        return self.t.regions[self.qi] if self.t is not None and self.qi is not None else None

    def open_question(self, ask=True):
        """Open the question chosen in the picker: the first time, ask whether to sort out blanks."""
        if self.worker is not None:
            return
        uid = self.cb_question.currentData()
        self.qi = next((i for i, r in enumerate(self.t.regions) if r.uid == uid), None) if (self.t and uid) else None
        if self.qi is None:
            self._show_page(PAGE_EMPTY)
            return
        if not hand.eligible_submissions(self.t):
            self._show_page(PAGE_EMPTY)
            unread = sum(1 for s in list_submissions_by_status(self.t.exam_name) if s.status == SUB_NEW)
            self.lbl_empty.setText(
                f"No tests of version {self.t.version_label} yet. " +
                (f"{unread} uploaded test{'s are' if unread != 1 else ' is'} not read yet: press “Read names” "
                 "in Scan & Names." if unread else "Upload the scans in Scan & Names first."))
            return
        stage = get_hand_stage(self.t.id, self.region.uid)
        if stage == "start":
            if ask:
                self._run_setup()
            return
        self._load_answers(stage)

    def choose_kind(self):
        """Ask the question's category (multiple choice: also its choice boxes) and save it.
        (kind, choices), or None when cancelled."""
        r = self.region
        kind, choices = get_hand_setup(self.t.id, r.uid)
        page = r.page if 0 <= r.page < self.t.page_count else 0
        tmpl = read_image(self.t.pages[page]) if self.t.page_count else None
        suggested = autogroup.KIND_CHOICE if tmpl is not None and marks.has_bubbles(tmpl, r.rect) else ""
        from ..students import trocr
        kind = group_setup.ask_kind(self, r.problem, kind, suggested, reader_ok=trocr.available(),
                                    page_img=tmpl, rect=r.rect, question_rect=r.question_rect)
        if kind is None:
            return None
        if kind == autogroup.KIND_CHOICE:
            if tmpl is None:
                QMessageBox.warning(self, "Hand Grade", "The blank exam page is missing, so the choices "
                                                        "can't be found.")
                return None
            choices = group_setup.edit_choices(self, tmpl, r.rect, r.problem, choices)
            if choices is None:
                return None
        set_hand_setup(self.t.id, r.uid, kind=kind, choices=choices if kind == autogroup.KIND_CHOICE else None)
        return kind, choices

    def _run_setup(self) -> bool:
        """Pick the category, then find blanks / suggest groups for the answers not grouped yet."""
        setup = self.choose_kind()
        if setup is None:
            return False
        self._start_setup(*setup)
        return True

    def _start_setup(self, kind, choices):
        stage = "group" if kind == autogroup.KIND_CHOICE else MODE_BLANKS
        set_hand_stage(self.t.id, self.region.uid, stage)
        self._load_answers(stage, auto=None if kind == autogroup.KIND_BLANK else kind, choices=choices)

    def change_kind(self):
        """Give the open question another category. Grading it starts over: once the new category
        is picked, its groups and hand grades are deleted (asked first; can't be undone)."""
        r = self.region
        if r is None or self.worker is not None:
            return
        kind = get_hand_setup(self.t.id, r.uid)[0]
        now = group_setup.KIND_TEXT[kind][0] if kind in group_setup.KIND_TEXT else "not chosen yet"
        msg = (f"Change the type of {r.problem} (now: {now})?\n\n{self._start_over_text()}"
               "\n\nNothing is deleted until you pick the new type.")
        yes, cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
        if QMessageBox.warning(self, "Change question type", msg, yes | cancel, cancel) != yes:
            return
        setup = self.choose_kind()
        if setup is None:
            return
        hand.start_over(self.t, self.qi)
        self.cur = None
        self._refresh_question_combo()
        self._start_setup(*setup)

    def _start_over_text(self) -> str:
        n_grades = hand.hand_grade_count(self.t, self.qi)
        n_groups = sum(1 for g in self._groups() if g["members"])
        return (f"Grading this question starts over in version {self.t.version_label}: its {n_groups} "
                f"group{'s' if n_groups != 1 else ''} and {n_grades} hand grade{'s' if n_grades != 1 else ''} "
                "will be deleted. This can't be undone.")

    def regrade_from_scratch(self):
        """Delete the open question's groups and hand grades (asked first) and set it up again with
        the same type: blanks / suggested groups, then group, confirm and grade."""
        r = self.region
        if r is None or self.worker is not None:
            return
        msg = f"Regrade {r.problem} from scratch?\n\n{self._start_over_text()}"
        yes, cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
        if QMessageBox.warning(self, "Regrade from scratch", msg, yes | cancel, cancel) != yes:
            return
        kind, choices = get_hand_setup(self.t.id, r.uid)
        n = hand.start_over(self.t, self.qi)
        self.cur = None
        self._refresh_question_combo()
        self._status(f"{r.problem}: groups and {n} hand grade{'s' if n != 1 else ''} deleted; grading starts over.")
        if kind:
            self._start_setup(kind, choices)
        else:
            self._run_setup()

    def suggest_groups(self):
        """Group stage → Suggest groups…: category (can be changed) and a new run over the answers
        that are not grouped or graded yet (late scans, after changing the choice boxes...)."""
        if self.region is not None and self.worker is None:
            self._run_setup()

    def _load_answers(self, stage: str, auto: str = None, choices=None):
        mode = auto or (MODE_BLANKS if stage == "blanks" else MODE_PLAIN)
        self._busy(True)
        self.lbl_progress.setText("Preparing the answers…")
        w = Worker(job_prepare, self.t, self.qi, mode, choices)
        self.worker = w
        w.progress.connect(self._on_progress)
        w.done.connect(lambda res: self._answers_ready(res, stage))
        w.failed.connect(self._answers_failed)
        w.start()

    def _on_progress(self, i, n, msg):
        self.progress.setRange(0, max(1, n)); self.progress.setValue(i)
        self.lbl_progress.setText(msg)

    def _busy(self, on: bool):
        for w in (self.cb_exam, self.cb_version, self.cb_question, self.btn_suggest):
            w.setEnabled(not on)
        for b in self.stage_btns:
            b.setEnabled(not on)
        self._update_actions(busy=on)
        self.progress.setVisible(on)
        self.btn_stop.setVisible(on)

    def _answers_failed(self, err):
        self.worker = None
        self._busy(False)
        self.lbl_progress.setText("")
        QMessageBox.critical(self, "Hand Grade", f"Could not prepare the answers:\n\n{err}")

    def _answers_ready(self, res, stage):
        answers, summary = res
        stopped = self.worker is not None and self.worker.cancelled
        self.worker = None
        self._busy(False)
        self.lbl_progress.setText("Stopped." if stopped else self._summary_text(summary))
        for grid in (self.grid_blanks, self.grid_group, self.grid_members):
            grid.set_aspect(self.region.rect if self.region else None)
        self.answers = {a.sub.id: a for a in answers}
        self.order = [a.sub.id for a in answers]
        self._confirm_pix.clear()
        if self.region is not None:
            hand.ensure_blank_group(self.t, self.qi)
        if stage == "blanks" and stopped:
            stage = "group"
            set_hand_stage(self.t.id, self.region.uid, stage)
        self._show_stage(stage)

    @staticmethod
    def _summary_text(s) -> str:
        if s is None:
            return ""
        if s.no_reader:
            return ("The handwriting reader could not be loaded, so no groups were suggested: "
                    "group the answers yourself.")
        if s.stopped:
            return "Stopped: no groups were suggested."
        n, g = s.grouped, s.groups
        text = (f"LMGrader suggested {g} group{'s' if g != 1 else ''} ({n} answer{'s' if n != 1 else ''})."
                if n else "No groups to suggest.")
        if s.unclear:
            text += f" {s.unclear} unclear answer{'s stay' if s.unclear != 1 else ' stays'} ungrouped."
        return text + (" Check each ⚑ group before grading it." if n else "")

    # ================= stages =================
    def _set_stage(self, stage: str):
        if self.region is None:
            return
        set_hand_stage(self.t.id, self.region.uid, stage)
        if stage == "blanks" and not any(a.p_not_blank is not None for a in self.answers.values()):
            self._load_answers("blanks")       # run the CNN now
            return
        self._show_stage(stage)

    def _stage_clicked(self, sid):
        self._set_stage(STAGES[sid - 1])

    def _show_stage(self, stage):
        page = STAGE_PAGE.get(stage, PAGE_GROUP)
        if page == PAGE_BLANKS:
            self._render_blanks()
        elif page == PAGE_GROUP:
            self._render_group()
        elif page == PAGE_CONFIRM:
            self._render_confirm()
        else:
            self._render_grade()
        self._show_page(page)

    def _show_page(self, page):
        self.stack.setCurrentIndex(page)
        for i, b in enumerate(self.stage_btns, start=1):
            b.setChecked(page == i)
            b.setVisible(page != PAGE_EMPTY)
        for arrow in self.stage_arrows:
            arrow.setVisible(page != PAGE_EMPTY)
        self._update_actions()
        self._update_header()

    def _update_actions(self, busy=None):
        """Header buttons. Edit rubric needs a picked question (its rubric can be edited before any
        scan exists); Change question type needs it open. Neither while answers are being prepared."""
        idle = not busy if busy is not None else self.worker is None
        self.btn_edit_rubric.setEnabled(idle and self.region is not None)
        for b in (self.btn_change_kind, self.btn_regrade):
            b.setEnabled(idle and self.stack.currentIndex() != PAGE_EMPTY)

    def _update_header(self):
        r = self.region
        if r is None or not self.answers:
            self.lbl_counts.setText("")
            return
        done = len(hand.hand_graded(self.t, self.qi))
        self.lbl_counts.setText(f"{done} / {len(self.answers)} graded by hand")

    def _caption(self, sid) -> str:
        return f"Test {self.nums.get(sid, '?')}"

    def _tooltip(self, sid) -> str:
        a = self.answers.get(sid)
        who = self.names.get(a.sub.student_id, "") if a else ""
        return (who or (a.sub.name_read if a else "")) or ""

    def _groups(self) -> List[dict]:
        return hand.groups(self.t, self.qi)

    # ---------- 1. blanks ----------
    def _render_blanks(self):
        in_group = {m for g in self._groups() for m in g["members"]}
        cands = [s for s in hand.predicted_blank(list(self.answers.values())) if s not in in_group]
        g = self.grid_blanks
        g.clear()
        for sid in cands:
            it = g.add_answer(sid, self.answers[sid].crop, self._caption(sid), self._tooltip(sid))
            it.setData(ROLE_ENTRY, True)          # True = blank
            self._paint_blank(it)
        n = len(cands)
        if n:
            self.lbl_blank_info.setText(f"<b>{n} answer{'s look' if n != 1 else ' looks'} blank.</b> Click any "
                                        "answer that is <b>not</b> blank to keep it out of the Blank group.")
        else:
            self.lbl_blank_info.setText("<b>No blank answers found.</b> Continue to group the answers.")
        self._update_blank_count()

    def _paint_blank(self, it):
        blank = bool(it.data(ROLE_ENTRY))
        sid = it.data(ROLE_SUB)
        it.setText(f"{self._caption(sid)}\n{'Blank' if blank else 'Not blank — graded normally'}")
        it.setBackground(QColor(theme.SURFACE_ALT) if blank else QColor(theme.WARN_SOFT))
        it.setForeground(QColor(theme.TEXT if blank else theme.WARN))

    def _toggle_blank(self, it):
        it.setData(ROLE_ENTRY, not bool(it.data(ROLE_ENTRY)))
        self._paint_blank(it)
        self._update_blank_count()

    def _blank_ids(self) -> List[int]:
        g = self.grid_blanks
        return [g.item(r).data(ROLE_SUB) for r in range(g.count()) if g.item(r).data(ROLE_ENTRY)]

    def _update_blank_count(self):
        n = len(self._blank_ids())
        self.lbl_blank_count.setText(f"{n} will be graded together as Blank" if n else "")
        self.btn_blank_ok.setText(f"Confirm {n} blank{'s' if n != 1 else ''}  →" if n else "Continue  →")

    def confirm_blanks(self):
        ids = self._blank_ids()
        if ids:
            blank = next((g for g in self._groups() if g["is_blank"]), None)
            if blank:
                set_hand_group_members(blank["id"], ids, add=True)
            else:
                add_hand_group(self.t.id, self.region.uid, hand.BLANK_LABEL, ids, is_blank=True)
        self._set_stage("group")

    # ---------- 2. group ----------
    def _render_group(self):
        groups = self._groups()
        cur = self.cb_filter.currentData()
        self.cb_filter.blockSignals(True)
        self.cb_filter.clear()
        self.cb_filter.addItem("Ungrouped answers", "ungrouped")
        self.cb_filter.addItem("All answers", "all")
        for g in groups:
            self.cb_filter.addItem(f"Group: {g['label']} ({len(g['members'])})", g["id"])
        self.cb_filter.setCurrentIndex(max(0, self.cb_filter.findData(cur)))
        self.cb_filter.blockSignals(False)
        sel = self.list_groups.currentItem().data(ROLE_ENTRY) if self.list_groups.currentItem() else None
        self.list_groups.blockSignals(True)
        self.list_groups.clear()
        for g in groups:
            n = len(g["members"])
            it = QListWidgetItem(f"{'⚑ ' if _to_check(g) else ''}{g['label']}  ·  {n} answer{'s' if n != 1 else ''}")
            it.setData(ROLE_ENTRY, g["id"])
            if _to_check(g):
                it.setForeground(QColor(theme.WARN))
                it.setToolTip("Suggested by LMGrader: check its answers, take out any that don't belong, "
                              "then press Looks right")
            elif g["is_blank"]:
                it.setForeground(QColor(theme.MUTED))
                it.setToolTip("Blank answers: select more answers, then click Blank to add them")
            self.list_groups.addItem(it)
            if g["id"] == sel:
                self.list_groups.setCurrentItem(it)
        self.list_groups.blockSignals(False)
        self._render_group_grid()

    def _render_group_grid(self):
        groups = self._groups()
        label_of = {m: g["label"] for g in groups for m in g["members"]}
        f = self.cb_filter.currentData()
        if f == "ungrouped":
            ids = [s for s in self.order if s not in label_of]
        elif f == "all" or f is None:
            ids = list(self.order)
        else:
            members = next((g["members"] for g in groups if g["id"] == f), [])
            ids = [s for s in self.order if s in members]
        g = self.grid_group
        g.clear()
        for sid in ids:
            cap = self._caption(sid) + (f"\n[{label_of[sid]}]" if sid in label_of and f == "all" else "")
            g.add_answer(sid, self.answers[sid].crop, cap, self._tooltip(sid))
        n_un = sum(1 for s in self.order if s not in label_of)
        n_check = sum(1 for g in groups if _to_check(g))
        kind = get_hand_setup(self.t.id, self.region.uid)[0] if self.region is not None else ""
        self.lbl_group_hint.setText(
            (f"{group_setup.KIND_TEXT[kind][0]} · " if kind in group_setup.KIND_TEXT else "")
            + (f"⚑ {n_check} suggested group{'s' if n_check != 1 else ''} to check: pick one in the list, take "
               "out answers that don't belong (select them, Take selection out of groups), then press Looks "
               "right. " if n_check else "")
            + (f"{n_un} ungrouped answer{'s' if n_un != 1 else ''} (graded one by one). " if f == "ungrouped" else "")
            + SELECT_HINT)
        self._update_group_buttons()

    def _resize_thumbs(self, w):
        """Both Size sliders (Group answers, Confirm groups) set every grid's thumbnails."""
        for sl in (self.sl_size, self.sl_confirm_size):
            if sl.value() != w:
                sl.blockSignals(True); sl.setValue(w); sl.blockSignals(False)
        for grid in (self.grid_group, self.grid_blanks, *self.confirm_grids):
            grid.set_thumb_width(w)

    def _update_group_buttons(self):
        has_sel = bool(self.grid_group.selectedItems())
        has_group = self.list_groups.currentItem() is not None
        gs = self._groups() if self.region is not None else []
        gid = self._selected_group_id()
        g = next((g for g in gs if g["id"] == gid), None)
        self.btn_new_group.setEnabled(has_sel)
        self.btn_add_to_group.setEnabled(has_sel and bool(gs))
        self.btn_ungroup_sel.setEnabled(has_sel)
        self.btn_select_none.setEnabled(has_sel)
        self.btn_rename_group.setEnabled(has_group)
        blank = g is not None and g["is_blank"]
        self.btn_delete_group.setEnabled(has_group and not (blank and not g["members"]))
        self.btn_delete_group.setText("Empty the Blank group" if blank else "Dissolve group")
        self.btn_check_group.setVisible(any(_to_check(x) for x in gs))
        self.btn_check_group.setEnabled(g is not None and _to_check(g))
        n = len(self.grid_group.selectedItems())
        self.btn_new_group.setText(f"New group from {n} selected" if n else "New group from selection")

    def _group_picked(self, row):
        it = self.list_groups.item(row)
        if it is not None:
            i = self.cb_filter.findData(it.data(ROLE_ENTRY))
            if i >= 0:
                self.cb_filter.setCurrentIndex(i)
        self._update_group_buttons()

    def _selected_group_id(self) -> Optional[int]:
        it = self.list_groups.currentItem()
        return it.data(ROLE_ENTRY) if it else None

    def new_group(self):
        ids = self.grid_group.selected_ids()
        if not ids:
            return
        n = sum(1 for g in self._groups() if not g["is_blank"]) + 1
        name, ok = QInputDialog.getText(self, "New group", f"Name for the group of {len(ids)} answers:",
                                        text=f"Group {n}")
        if not ok:
            return
        add_hand_group(self.t.id, self.region.uid, name.strip() or f"Group {n}", ids)
        self._render_group()

    def check_group(self):
        """The selected suggested group is right; the next one to check is selected."""
        gid = self._selected_group_id()
        if not gid:
            return
        update_hand_group(gid, checked=True)
        gs = self._groups()
        at = next((k for k, g in enumerate(gs) if g["id"] == gid), 0)
        nxt = next((gs[(at + k) % len(gs)]["id"] for k in range(1, len(gs) + 1)
                    if _to_check(gs[(at + k) % len(gs)])), None)
        self._render_group()
        if nxt is not None:
            row = next((r for r in range(self.list_groups.count())
                        if self.list_groups.item(r).data(ROLE_ENTRY) == nxt), None)
            if row is not None:
                self.list_groups.setCurrentRow(row)

    def _fill_add_menu(self):
        """Add selection to group: every group of the question, Blank first."""
        self.menu_add.clear()
        for g in self._groups() if self.region is not None else []:
            n = len(g["members"])
            act = self.menu_add.addAction(f"{g['label']}  ({n} answer{'s' if n != 1 else ''})")
            act.triggered.connect(lambda _=False, gid=g["id"]: self.add_selection_to(gid))

    def add_selection_to(self, gid: int):
        """The selected answers join group gid (leaving any other group)."""
        ids = self.grid_group.selected_ids()
        if gid and ids:
            set_hand_group_members(gid, ids, add=True)
            g = next((g for g in self._groups() if g["id"] == gid), None)
            self._render_group()
            if g is not None:
                self._status(f"{len(ids)} answer{'s' if len(ids) != 1 else ''} added to {g['label']}.")

    def eventFilter(self, obj, e):
        """A click on a group while answers are selected adds them to it (the grid keeps showing
        what it showed); without a selection the click just picks the group (shows its answers)."""
        if (e.type() == QEvent.Type.MouseButtonPress and obj is self.list_groups.viewport()
                and e.button() == Qt.MouseButton.LeftButton and self.grid_group.selectedItems()):
            it = self.list_groups.itemAt(e.position().toPoint())
            if it is not None:
                self.add_selection_to(it.data(ROLE_ENTRY))
                return True
        return super().eventFilter(obj, e)

    def ungroup_selection(self):
        ids = self.grid_group.selected_ids()
        if ids:
            remove_from_hand_groups(self.t.id, self.region.uid, ids)
            self._render_group()

    def rename_group(self):
        gid = self._selected_group_id()
        g = next((g for g in self._groups() if g["id"] == gid), None)
        if g is None:
            return
        name, ok = QInputDialog.getText(self, "Rename group", "Group name:", text=g["label"])
        if ok and name.strip():
            update_hand_group(gid, label=name.strip())
            self._render_group()

    def dissolve_group(self):
        """Its answers become ungrouped and lose their hand grades (asked first when it has
        some); the Blank group stays (empty)."""
        gid = self._selected_group_id()
        g = next((g for g in self._groups() if g["id"] == gid), None)
        if g is None:
            return
        n = hand.hand_grade_count(self.t, self.qi, set(g["members"]))
        if n:
            what = "Empty the Blank group" if g["is_blank"] else f"Dissolve {g['label']}"
            m = len(g["members"])
            msg = (f"{what}? Its {m} answer{'s become' if m != 1 else ' becomes'} ungrouped and {n} hand "
                   f"grade{'s are' if n != 1 else ' is'} deleted: {'they are' if m != 1 else 'it is'} graded again.")
            yes, cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
            if QMessageBox.question(self, what, msg, yes | cancel, cancel) != yes:
                return
        hand.dissolve_group(self.t, self.qi, g)
        self._render_group()
        self._refresh_question_combo()
        if n:
            self._status(f"{n} hand grade{'s' if n != 1 else ''} deleted.")

    # ---------- 3. confirm groups ----------
    def _render_confirm(self):
        """Every group that has answers: its name and count, then all its answers."""
        lay = self.confirm_layout
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.hide(); w.setParent(None); w.deleteLater()
        self.confirm_grids = []
        groups = [g for g in self._groups() if g["members"]]
        graded = hand.hand_graded(self.t, self.qi)
        rect = self.region.rect if self.region is not None else None
        for g in groups:
            n = len(g["members"])
            done = sum(1 for m in g["members"] if m in graded)
            head = label(f"{'⚑ ' if _to_check(g) else ''}<b>{escape(g['label'])}</b>  ·  "
                         f"{n} answer{'s' if n != 1 else ''}"
                         + (f"  ·  {done} graded" if done else "")
                         + ("  ·  suggested by LMGrader, not checked yet" if _to_check(g) else ""))
            head.setTextFormat(Qt.TextFormat.RichText)
            if _to_check(g):
                head.setStyleSheet(f"color: {theme.WARN};")
            lay.addWidget(head)
            grid = FitGrid(self._confirm_pix)
            grid.set_aspect(rect)
            grid.set_thumb_width(self.sl_confirm_size.value())
            for sid in g["members"]:
                if sid in self.answers:
                    grid.add_answer(sid, self.answers[sid].crop, self._caption(sid), self._tooltip(sid))
            grid.itemSelectionChanged.connect(self._update_confirm_buttons)
            lay.addWidget(grid)
            self.confirm_grids.append(grid)
        lay.addStretch()
        for grid in self.confirm_grids:
            grid.fit()
        grouped = {m for g in groups for m in g["members"]}
        n_un = sum(1 for s in self.order if s not in grouped)
        n_check = sum(1 for g in groups if _to_check(g))
        un = (f" {n_un} answer{'s are' if n_un != 1 else ' is'} not in a group (graded one by one)."
              if n_un else "")
        if groups:
            self.lbl_confirm_info.setText(
                f"<b>{len(groups)} group{'s' if len(groups) != 1 else ''}.</b> Click every answer that doesn't "
                "belong in its group (click again to unselect), then <b>Move selected to ungrouped</b>." + un
                + (f" Groups are right also checks the {n_check} ⚑ suggested group{'s' if n_check != 1 else ''}."
                   if n_check else ""))
        else:
            self.lbl_confirm_info.setText("<b>No groups.</b>" + un + " Group answers in step 2, or grade them now.")
        self._update_confirm_buttons()

    def _confirm_selected(self) -> List[int]:
        return [sid for grid in self.confirm_grids for sid in grid.selected_ids()]

    def _confirm_select_none(self):
        for grid in self.confirm_grids:
            grid.clearSelection()

    def _update_confirm_buttons(self):
        n = len(self._confirm_selected())
        self.btn_confirm_ungroup.setEnabled(bool(n))
        self.btn_confirm_none.setEnabled(bool(n))
        self.btn_confirm_ungroup.setText(f"Move {n} selected to ungrouped" if n else "Move selected to ungrouped")

    def confirm_ungroup(self):
        """The selected answers leave their groups (they are graded one by one)."""
        ids = self._confirm_selected()
        if not ids or self.region is None:
            return
        remove_from_hand_groups(self.t.id, self.region.uid, ids)
        self._render_confirm()           # the page keeps its scroll position
        self._status(f"{len(ids)} answer{'s' if len(ids) != 1 else ''} moved to ungrouped.")

    def confirm_groups(self):
        """Every group was seen: the suggested ones count as checked; on to grading."""
        if self.region is None:
            return
        for g in self._groups():
            if _to_check(g):
                update_hand_group(g["id"], checked=True)
        self._set_stage("grade")

    # ---------- 4. grade ----------
    def _render_grade(self):
        """Queue: groups first (blank group at the top; empty groups left out), then every
        ungrouped answer."""
        graded = hand.hand_graded(self.t, self.qi)
        groups = self._groups()
        grouped = {m for g in groups for m in g["members"]}
        self.queue = ([("group", g["id"]) for g in groups if g["members"]]
                      + [("answer", s) for s in self.order if s not in grouped])
        keep = self.cur
        self.list_queue.blockSignals(True)
        self.list_queue.clear()
        for kind, key in self.queue:
            it = QListWidgetItem(self._queue_text(kind, key, groups, graded))
            it.setData(ROLE_ENTRY, (kind, key))
            done = self._entry_done(kind, key, groups, graded)
            it.setForeground(QColor(theme.OK if done else theme.TEXT))
            self.list_queue.addItem(it)
        self.list_queue.blockSignals(False)
        n_done = sum(self._entry_done(k, x, groups, graded) for k, x in self.queue)
        self.lbl_queue.setText(f"{n_done} of {len(self.queue)} done · {len(graded)} / {len(self.answers)} answers")
        row = next((i for i, e in enumerate(self.queue) if e == keep), None)
        if row is None:
            row = next((i for i, (k, x) in enumerate(self.queue) if not self._entry_done(k, x, groups, graded)), 0)
        if self.queue:
            self.list_queue.setCurrentRow(row)
            self._queue_picked(row)
        self._update_header()

    def _entry_done(self, kind, key, groups, graded) -> bool:
        if kind == "group":
            g = next((g for g in groups if g["id"] == key), None)
            return bool(g and g["members"] and all(m in graded for m in g["members"]))
        return key in graded

    def _queue_text(self, kind, key, groups, graded) -> str:
        r = self.region
        if kind == "group":
            g = next(g for g in groups if g["id"] == key)
            n = len(g["members"])
            done = self._entry_done(kind, key, groups, graded)
            score = graded[g["members"][0]].score if done else None
            return (f"{'✓' if done else '○'}  {'⚑ ' if _to_check(g) else ''}{g['label']}  ·  "
                    f"{n} answer{'s' if n != 1 else ''}"
                    + (f"  ·  {score:g}/{r.points:g}" if score is not None else ""))
        gr = graded.get(key)
        return f"{'✓' if gr else '○'}  {self._caption(key)}" + (f"  ·  {gr.score:g}/{r.points:g}" if gr else "")

    def _queue_picked(self, row):
        if not (0 <= row < len(self.queue)):
            return
        self.cur = self.queue[row]
        kind, key = self.cur
        r = self.region
        groups = self._groups()
        graded = hand.hand_graded(self.t, self.qi)
        items, comment = [], ""
        self.lbl_ai.setText("")
        if kind == "group":
            g = next(g for g in groups if g["id"] == key)
            n = len(g["members"])
            self.lbl_answer_title.setText(f"{g['label']} — {n} answer{'s' if n != 1 else ''}")
            gm = self.grid_members
            gm.clear()
            for sid in g["members"]:
                if sid in self.answers:
                    gm.add_answer(sid, self.answers[sid].crop, self._caption(sid), self._tooltip(sid))
            self.answer_stack.setCurrentIndex(1)
            items, comment = g["items"], g["comment"]
            first = graded.get(g["members"][0]) if g["members"] else None
            if not items and first is not None:
                items, comment = first.rubric_items, first.comment
            self.btn_full_page.setEnabled(False)
            self.lbl_applies.setText(f"Applies to all {n} answers in this group."
                                     + (" ⚑ Suggested group: check that every answer belongs before saving."
                                        if _to_check(g) else ""))
        else:
            a = self.answers[key]
            who = self._tooltip(key)
            self.lbl_answer_title.setText(self._caption(key) + (f"  ·  {who}" if who else ""))
            self.single_img.set_image(read_image(a.crop) if a.crop else None)
            self.answer_stack.setCurrentIndex(0)
            g = graded.get(key)
            if g is not None:
                items, comment = g.rubric_items, g.comment
            else:
                ai = region_grades(self.t.id, self.qi).get(key)
                if ai is not None and not ai.human_graded:
                    self.lbl_ai.setText(f"AI grade: {ai.score:g} / {ai.max_points:g} — {ai.feedback}"[:400]
                                        + "  (your grade replaces it)")
            self.btn_full_page.setEnabled(bool(a.crop))
            self.lbl_applies.setText("")
        self._render_items(items, comment)
        done = self._entry_done(kind, key, groups, graded)
        self.btn_clear.setEnabled(done)

    def _render_items(self, checked: List[str], comment: str):
        r = self.region
        rub = r.rubric
        self._loading = True
        while self.items_layout.count():
            w = self.items_layout.takeAt(0).widget()
            if w is not None:
                w.hide()                 # deleteLater alone leaves it painted until the next loop
                w.setParent(None)
                w.deleteLater()
        self.item_rows = []
        for n, it in enumerate(rub.items, start=1):
            row = ItemRow(n, it, self._update_score)
            row.chk.setChecked(it.id in set(checked or []))
            self.items_layout.addWidget(row)
            self.item_rows.append(row)
        self.items_layout.addStretch()
        self.lbl_no_items.setVisible(not rub.items)
        self.lbl_scoring.setText(f"Deduct: starts at {r.points:g}, checked items take points off." if rub.negative
                                 else "Add up: starts at 0, checked items add their points.")
        exp = rub.expected_answer.strip() or r.answer_key.strip()
        self.lbl_expected.setText(latex_html(f"Expected: {exp}", theme.MUTED,
                                             QFontInfo(self.lbl_expected.font()).pixelSize()) if exp else "")
        self.lbl_expected.setVisible(bool(exp))
        self.in_comment.setPlainText(comment or "")
        self._loading = False
        self.btn_save.setEnabled(bool(rub.items))
        self._update_score()

    def checked_items(self) -> List[str]:
        return [row.item.id for row in self.item_rows if row.chk.isChecked()]

    def toggle_item(self, k: int):
        if self.stack.currentIndex() == PAGE_GRADE and 0 <= k < len(self.item_rows):
            self.item_rows[k].chk.toggle()

    def _update_score(self):
        r = self.region
        if self._loading or r is None:
            return
        s = r.rubric.score(self.checked_items(), r.points)
        self.lbl_score.setText(f"{s:g} / {r.points:g}")

    def save_current(self):
        if self.stack.currentIndex() != PAGE_GRADE or self.cur is None or not self.region.rubric.items:
            return
        kind, key = self.cur
        items, comment = self.checked_items(), self.in_comment.toPlainText().strip()
        if kind == "group":
            g = next((g for g in self._groups() if g["id"] == key), None)
            if g is None:
                return
            crops = {sid: self.answers[sid].crop for sid in g["members"] if sid in self.answers}
            hand.grade_group(self.t, self.qi, g, items, comment, crops)
        else:
            hand.save_hand_grade(self.t, self.qi, key, items, comment, crop=self.answers[key].crop)
        self._advance_after_save()

    def _advance_after_save(self):
        groups = self._groups()
        graded = hand.hand_graded(self.t, self.qi)
        start = self.queue.index(self.cur) if self.cur in self.queue else -1
        n = len(self.queue)
        nxt = next((self.queue[(start + k) % n] for k in range(1, n + 1)
                    if not self._entry_done(*self.queue[(start + k) % n], groups, graded)), None)
        self.cur = nxt or self.cur
        self._render_grade()
        self._refresh_question_combo()
        if nxt is None:
            self._next_question()

    def _next_question(self):
        """Every answer to the open question is graded: say so before anything else opens, then
        go to the next question (its category is asked the first time) unless the user stays."""
        done = self.region.problem
        i = self.cb_question.currentIndex()
        graded = f"Every answer to {done} is graded by hand"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(f"{done} finished")
        if i + 1 >= self.cb_question.count():
            box.setText(f"{graded}.\n\nThat was the last question.")
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            self._status(f"{graded}. That was the last question.")
            return
        uid = self.cb_question.itemData(i + 1)
        nxt = next((r.problem for r in self.t.regions if r.uid == uid), "the next question")
        box.setText(f"{graded}.\n\nNext is {nxt}.")
        go = box.addButton(f"Go to {nxt}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(f"Stay on {done}", QMessageBox.ButtonRole.RejectRole)     # Esc too
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is not go:
            self._status(f"{graded}.")
            return
        self.cb_question.setCurrentIndex(i + 1)
        self._status(f"{graded}: on to {nxt}.")
        self.cur = None
        self.open_question()

    def clear_current(self):
        if self.cur is None:
            return
        kind, key = self.cur
        graded = hand.hand_graded(self.t, self.qi)
        if kind == "group":
            g = next((g for g in self._groups() if g["id"] == key), None)
            sids = g["members"] if g else []
            if g:
                update_hand_group(g["id"], items=[], comment="")
        else:
            sids = [key]
        targets = [graded[s] for s in sids if s in graded]
        if not targets or QMessageBox.question(
                self, "Clear grade", f"Delete the hand grade of {len(targets)} answer{'s' if len(targets) != 1 else ''}?"
        ) != QMessageBox.StandardButton.Yes:
            return
        for gr in targets:
            delete_grade(gr.id)
            refresh_status(gr.submission_id, self.t)
        self._render_grade()
        self._refresh_question_combo()

    def _step(self, d):
        if self.stack.currentIndex() == PAGE_GRADE and self.queue:
            row = max(0, min(len(self.queue) - 1, self.list_queue.currentRow() + d))
            self.list_queue.setCurrentRow(row)

    def _refresh_question_combo(self):
        i = self.cb_question.currentIndex()
        if i > 0 and self.region is not None:
            total = len(self.answers)
            done = len(hand.hand_graded(self.t, self.qi))
            r = self.region
            self.cb_question.setItemText(i, f"{'✓ ' if total and done >= total else ''}{r.problem}  ·  "
                                            f"{r.points:g} pts  ·  {done}/{total} by hand")
        self._update_header()

    def show_full_page(self):
        if self.cur is None or self.cur[0] != "answer":
            return
        a = self.answers.get(self.cur[1])
        r = self.region
        img = hand.aligned_page(self.t, a.sub, r.page) if a else None
        if img is None:
            return
        dlg = getattr(self, "_page_dlg", None)
        if dlg is None:
            dlg = self._page_dlg = QDialog(self)
            dlg.resize(900, 1000)
            lay = QVBoxLayout(dlg)
            dlg.viewer = PageViewer()
            dlg.viewer.canvas.draw_enabled = False
            lay.addWidget(dlg.viewer)
        dlg.setWindowTitle(f"{self._caption(a.sub.id)} — page {r.page + 1}")
        dlg.show(); dlg.raise_()
        dlg.viewer.set_image(img)
        dlg.viewer.canvas.set_overlays([Overlay(tuple(r.rect), QColor(theme.ACCENT), r.problem)])
        s = dlg.viewer.canvas.scale()
        x, y, w, h = r.rect
        dlg.viewer.ensureVisible(int((x + w / 2) * s), int((y + h / 2) * s), int(w * s / 2) + 40, int(h * s / 2) + 80)

    # ================= rubric =================
    def edit_rubric(self):
        """Edit the question's rubric here (e.g. add an item while grading). Saved to
        the exam version; the Exams & Templates editor shows it too."""
        if self.region is None:
            return
        if self.template_view is not None and hasattr(self.template_view, "flush"):
            self.template_view.flush()
        t = load_template(self.t.id)
        r = t.regions[self.qi]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Rubric — {r.problem} ({r.points:g} pts)")
        dlg.resize(640, 480)
        lay = QVBoxLayout(dlg)
        ed = RubricEditor()
        ed.set_rubric(r.rubric, r.points)
        lay.addWidget(ed, 1)
        lay.addWidget(label("Answers you already graded update to the new rubric, unless their score or "
                            "feedback was changed in Review.", "muted"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        r.rubric = ed.rubric()
        r.rubric.max_points = r.points
        persist(t)
        self.t = t
        tv = self.template_view
        if tv is not None and getattr(tv, "t", None) is not None and tv.t.id == t.id:
            sel = tv.sel
            tv.load(t.id)
            if sel is not None and sel < len(t.regions):
                tv.region_list.setCurrentRow(sel)
        if self.stack.currentIndex() == PAGE_GRADE:
            self._render_grade()

    def _status(self, msg):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(msg, 5000)

    def shutdown(self, timeout_ms=3000):
        w = self.worker
        if w is not None:
            w.stop()
            w.wait(timeout_ms)
