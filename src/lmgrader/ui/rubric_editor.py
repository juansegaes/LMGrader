"""Structured rubric editor: scoring mode, expected answer and rubric items (points +
description) that can be added, deleted and reordered (drag the ⠿ handle, or ↑ / ↓)."""
from typing import Optional
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
                             QAbstractItemView, QDoubleSpinBox, QLineEdit, QComboBox, QPushButton, QApplication,
                             QPlainTextEdit)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QEvent
from PyQt6.QtGui import QColor

from ..config import UI_CONFIG, save_settings
from ..models import Rubric, RubricItem, SCORING_POSITIVE, SCORING_NEGATIVE
from . import theme
from .theme import button, label

COL_HANDLE, COL_POINTS, COL_DESC, COL_DEL = range(4)


def last_scoring() -> str:
    """The scoring the professor picked last (UI_CONFIG["rubric_scoring"]); a rubric without
    items starts with it."""
    s = UI_CONFIG.get("rubric_scoring")
    return s if s in (SCORING_POSITIVE, SCORING_NEGATIVE) else SCORING_POSITIVE


def remember_scoring(scoring: str):
    if scoring in (SCORING_POSITIVE, SCORING_NEGATIVE) and UI_CONFIG.get("rubric_scoring") != scoring:
        UI_CONFIG["rubric_scoring"] = scoring
        save_settings()

class ReorderTable(QTableWidget):
    """Table whose rows are reordered by dragging; it only reports the move (rowMoved), the owner
    reorders its data and rebuilds the rows."""
    rowMoved = pyqtSignal(int, int)     # from row, to row (final position)

    def __init__(self, rows: int, cols: int):
        super().__init__(rows, cols)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def drop_target(self, src: int, y: int) -> int:
        """Final row of a row dragged from `src` and dropped at viewport height y."""
        idx = self.indexAt(QPoint(4, y))
        if not idx.isValid():
            return self.rowCount() - 1
        dst = idx.row() + (1 if y > self.visualRect(idx).center().y() else 0)
        return dst - 1 if dst > src else dst

    def dropEvent(self, e):
        if e.source() is not self:
            e.ignore()
            return
        src = self.currentRow()
        dst = self.drop_target(src, int(e.position().y()))
        e.setDropAction(Qt.DropAction.IgnoreAction)   # the owner moves the data, Qt moves nothing
        e.accept()
        if src >= 0 and dst != src:
            self.rowMoved.emit(src, dst)

class RubricEditor(QWidget):
    """Edits a copy of a Rubric; `changed` is emitted after every edit (read it with rubric())."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rubric = Rubric()
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(label("Scoring"))
        self.cb_scoring = QComboBox()
        self.cb_scoring.addItem("Add up (from 0)", SCORING_POSITIVE)
        self.cb_scoring.addItem("Deduct (from max)", SCORING_NEGATIVE)   # "max" becomes the points (_update_total)
        self.cb_scoring.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.cb_scoring.setToolTip("Add up: the score starts at 0 and every checked item adds its points.\n"
                                   "Deduct: the score starts at the max points and every checked item takes "
                                   "its points off.\nEither way it stays between 0 and the max points.")
        top.addWidget(self.cb_scoring)
        top.addStretch()
        lay.addLayout(top)
        self.lbl_total = label("", "muted")
        self.lbl_total.setWordWrap(True)
        lay.addWidget(self.lbl_total)

        self.in_answer = QLineEdit()
        self.in_answer.setPlaceholderText("Expected answer (optional, LaTeX allowed)")
        self.in_answer.setToolTip("Shown to you when grading by hand and given to the AI grader.")
        lay.addWidget(self.in_answer)

        self.table = ReorderTable(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Points", "Rubric item", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_HANDLE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_POINTS, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_DESC, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_DEL, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_HANDLE, 22)
        self.table.setColumnWidth(COL_POINTS, 76)
        self.table.setColumnWidth(COL_DEL, 32)
        self.table.verticalHeader().setDefaultSectionSize(54)    # two lines of description
        self.table.setMinimumHeight(150)
        lay.addWidget(self.table, 1)
        self.lbl_empty = label("No rubric items yet. Add them here or generate them with the LLM.", "muted")
        self.lbl_empty.setWordWrap(True)
        lay.addWidget(self.lbl_empty)

        row = QHBoxLayout()
        self.btn_add = button("+ Add item", tooltip="Add a rubric item (points + what earns or loses them)")
        self.btn_up = button("↑", tooltip="Move the selected item up (or drag its ⠿ handle)")
        self.btn_down = button("↓", tooltip="Move the selected item down (or drag its ⠿ handle)")
        for b in (self.btn_up, self.btn_down):
            b.setFixedWidth(40)
        row.addWidget(self.btn_add); row.addStretch(); row.addWidget(self.btn_up); row.addWidget(self.btn_down)
        lay.addLayout(row)

        self.cb_scoring.currentIndexChanged.connect(self._scoring_changed)
        self.in_answer.textEdited.connect(self._answer_edited)
        self.btn_add.clicked.connect(lambda: self.add_item())
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.table.rowMoved.connect(self.move_item)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._render()

    # ---------- data ----------
    def set_rubric(self, rubric: Optional[Rubric], max_points: float = None):
        """Show a copy of `rubric` (None: empty). Emits nothing. Without items its scoring does
        nothing yet, so it starts as the scoring picked last (the next question's rubric)."""
        self._rubric = rubric.copy() if rubric is not None else Rubric()
        if not self._rubric.items:
            self._rubric.scoring = last_scoring()
        if max_points is not None:
            self._rubric.max_points = max_points
        self._render()

    def rubric(self) -> Rubric:
        return self._rubric.copy()

    def set_max_points(self, p: float):
        self._rubric.max_points = p
        self._update_total()

    def _emit(self):
        self._update_total()
        self.changed.emit()

    # ---------- edits ----------
    def add_item(self, points: float = None, description: str = "") -> int:
        if points is None:
            points = -1.0 if self._rubric.negative else 1.0
        self._rubric.items.append(RubricItem(points, description))
        row = len(self._rubric.items) - 1
        self._render(select=row)
        w = self.table.cellWidget(row, COL_DESC)
        if w is not None and not description:
            w.setFocus()
        self._emit()
        return row

    def delete_item(self, row: int):
        if 0 <= row < len(self._rubric.items):
            del self._rubric.items[row]
            self._render(select=min(row, len(self._rubric.items) - 1))
            self._emit()

    def move_item(self, src: int, dst: int):
        items = self._rubric.items
        if not (0 <= src < len(items)) or not (0 <= dst < len(items)) or src == dst:
            return
        items.insert(dst, items.pop(src))
        self._render(select=dst)
        self._emit()

    def selected_row(self) -> int:
        """The row being edited (focus inside it) or selected; -1 if none."""
        fw = QApplication.focusWidget()
        for r in range(self.table.rowCount()):
            for c in (COL_POINTS, COL_DESC):
                w = self.table.cellWidget(r, c)
                if w is not None and fw is not None and (w is fw or w.isAncestorOf(fw)):
                    return r
        return self.table.currentRow()

    def move_selected(self, step: int):
        r = self.selected_row()
        if r >= 0:
            self.move_item(r, r + step)

    def _scoring_changed(self):
        if self._loading:
            return
        self._rubric.scoring = self.cb_scoring.currentData()
        remember_scoring(self._rubric.scoring)
        self._emit()

    def _answer_edited(self, text):
        self._rubric.expected_answer = text
        self._emit()

    def _points_edited(self, row, v):
        if self._loading or row >= len(self._rubric.items):
            return
        self._rubric.items[row].points = float(v)
        self._emit()

    def _desc_edited(self, row, text):
        if self._loading or row >= len(self._rubric.items):
            return
        self._rubric.items[row].description = text
        self._emit()

    # ---------- view ----------
    def _render(self, select: int = None):
        self._loading = True
        r = self._rubric
        self.cb_scoring.setCurrentIndex(max(0, self.cb_scoring.findData(r.scoring)))
        if self.in_answer.text() != r.expected_answer:
            self.in_answer.setText(r.expected_answer)
            self.in_answer.setCursorPosition(0)       # show the start, not the end
        t = self.table
        t.setRowCount(0)
        for i, it in enumerate(r.items):
            t.insertRow(i)
            handle = QTableWidgetItem("⠿")
            handle.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
            handle.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            handle.setForeground(QColor(theme.MUTED))
            handle.setToolTip("Drag to reorder")
            t.setItem(i, COL_HANDLE, handle)
            sp = QDoubleSpinBox()
            sp.setRange(-1000, 1000); sp.setDecimals(2); sp.setSingleStep(0.5)
            sp.setValue(it.points)
            sp.setToolTip("Points this item adds (negative: points taken off)")
            sp.valueChanged.connect(lambda v, row=i: self._points_edited(row, v))
            t.setCellWidget(i, COL_POINTS, sp)
            ed = QPlainTextEdit(it.description)
            ed.setPlaceholderText("What earns (or loses) these points, e.g. Correct derivative")
            ed.setTabChangesFocus(True)
            ed.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            ed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ed.setStyleSheet("QPlainTextEdit { padding: 1px 4px; }")
            ed.textChanged.connect(lambda row=i, e=ed: self._desc_edited(row, e.toPlainText()))
            t.setCellWidget(i, COL_DESC, ed)
            rm = QPushButton("✕")
            rm.setProperty("kind", "ghost")
            rm.setToolTip("Delete this item")
            rm.setFixedWidth(30)
            rm.setStyleSheet("padding: 0px;")
            rm.clicked.connect(lambda _, row=i: self.delete_item(row))
            t.setCellWidget(i, COL_DEL, rm)
            for w in (sp, ed):
                w.setProperty("rubric_row", i)
                w.installEventFilter(self)       # editing a row selects it (for ↑ / ↓)
            # an empty slot behind the widgets keeps the row draggable from the handle only
            for c in (COL_POINTS, COL_DESC, COL_DEL):
                blank = QTableWidgetItem("")
                blank.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                t.setItem(i, c, blank)
        if select is not None and 0 <= select < t.rowCount():
            t.selectRow(select)
        # show every item (up to 8) without scrolling the table inside the scrolling panel
        t.setMinimumHeight(min(8, max(2, len(r.items))) * t.verticalHeader().defaultSectionSize()
                           + t.horizontalHeader().height() + 6)
        self.lbl_empty.setVisible(not r.items)
        self._loading = False
        self._update_total()
        self._update_buttons()

    def _update_total(self):
        r = self._rubric
        mx = r.max_points
        self.cb_scoring.setItemText(self.cb_scoring.findData(SCORING_NEGATIVE),
                                    f"Deduct (from {mx:g})" if mx > 0 else "Deduct (from max)")
        if r.negative:
            text = f"Starts at {mx:g} · {len(r.items)} deduction{'s' if len(r.items) != 1 else ''}"
            warn = any(it.points > 0 for it in r.items)
            tip = "Deduction items should have negative points." if warn else ""
        else:
            got = sum(it.points for it in r.items if it.points > 0)
            text = f"Items add up to {got:g} / {mx:g}"
            warn = bool(r.items) and abs(got - mx) > 1e-6
            tip = ("The positive items don't add up to the question's max points; the score is "
                   "capped at the max." if warn else "")
        self.lbl_total.setText(("⚠ " if warn else "") + text)
        self.lbl_total.setStyleSheet(f"color: {theme.WARN};" if warn else "")
        self.lbl_total.setToolTip(tip)

    def _update_buttons(self):
        r = self.table.currentRow()
        n = self.table.rowCount()
        enabled = self.isEnabled()
        self.btn_up.setEnabled(enabled and r > 0)
        self.btn_down.setEnabled(enabled and 0 <= r < n - 1)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.FocusIn and obj.property("rubric_row") is not None:
            row = int(obj.property("rubric_row"))
            if self.table.currentRow() != row:
                self.table.selectRow(row)
        return super().eventFilter(obj, e)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.EnabledChange:
            self._update_buttons()
