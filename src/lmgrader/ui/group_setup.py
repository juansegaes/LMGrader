"""Hand Grade setup dialogs: a question's grouping category and its multiple choice boxes."""
from typing import List, Optional

import cv2
import numpy as np
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QRadioButton, QButtonGroup, QDialogButtonBox,
                             QListWidget, QListWidgetItem, QLabel, QWidget)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QKeySequence, QShortcut

from ..grading import marks
from ..grading.autogroup import KIND_BLANK, KIND_SHORT, KIND_CHOICE
from . import theme
from .theme import button, label
from .widgets import PageViewer, Overlay, np_to_pixmap

PREVIEW_W, PREVIEW_H = 600, 300      # the question picture in ask_kind (scaled down to fit)

KIND_TEXT = {
    KIND_BLANK: ("Blank answers only",
                 "A small model finds the empty answer boxes; you group the other answers yourself."),
    KIND_SHORT: ("Short answer",
                 "Blank answers are found, then each answer is read on this computer (TrOCR) and answers "
                 "that read the same are grouped. Best for a word or a number; longer work and formulas "
                 "stay ungrouped."),
    KIND_CHOICE: ("Multiple choice",
                  "You check the choice boxes once (printed bubbles or answers); every test is grouped by "
                  "the choice it marked: A, B, A + C (more than one marked), Blank."),
}

def question_view(page_img: np.ndarray, rect, question_rect=None) -> np.ndarray:
    """The answer box zoomed out on the blank exam page: the whole page width, room above the box
    for the question (and the question area, if the question has one), the box outlined."""
    H, W = page_img.shape[:2]
    x, y, w, h = (int(v) for v in rect)
    top, bottom = y - max(2 * h, H // 8), y + h + max(h // 3, H // 40)
    if question_rect:
        qy, qh = int(question_rect[1]), int(question_rect[3])
        top, bottom = min(top, qy - H // 80), max(bottom, qy + qh + H // 80)
    y0, y1 = max(0, top), min(H, bottom)
    view = page_img[y0:y1].copy()
    if view.ndim == 2:
        view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)
    c = QColor(theme.ACCENT)
    cv2.rectangle(view, (x, y - y0), (x + w, y + h - y0), (c.blue(), c.green(), c.red()), max(2, W // 250))
    return view

def ask_kind(parent, problem: str, current: str = "", suggested: str = "", reader_ok: bool = True,
             page_img: np.ndarray = None, rect=None, question_rect=None) -> Optional[str]:
    """The question's grouping category, or None (cancelled). With the blank page and the answer
    box, the question is shown zoomed out above the choices (question_view)."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Group the answers to {problem}")
    dlg.setMinimumWidth(520)
    lay = QVBoxLayout(dlg)
    head = label(f"<b>What kind of question is {problem}?</b> LMGrader suggests groups of answers that "
                 "get the same grade; you check them, then grade each group once.")
    head.setWordWrap(True)
    lay.addWidget(head)
    if page_img is not None and rect:
        pm = np_to_pixmap(question_view(page_img, rect, question_rect))
        pic = QLabel()
        pic.setObjectName("kind_preview")
        pic.setPixmap(pm.scaled(PREVIEW_W, PREVIEW_H, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation))
        pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pic.setStyleSheet(f"border: 1px solid {theme.BORDER}; background: white;")
        pic.setToolTip(f"{problem} on the blank exam; its answer box is outlined")
        lay.addWidget(pic)
        cap = label(f"{problem} on the blank exam (answer box outlined)", "muted")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(cap)
    grp = QButtonGroup(dlg)
    pick = current or suggested or KIND_BLANK
    for kind in (KIND_BLANK, KIND_SHORT, KIND_CHOICE):
        title, desc = KIND_TEXT[kind]
        rb = QRadioButton(title + ("  (suggested: the page has bubbles)" if kind == suggested == KIND_CHOICE
                                   and not current else ""))
        rb.setObjectName(f"kind_{kind}")
        f = rb.font(); f.setBold(True); rb.setFont(f)
        grp.addButton(rb)
        rb.setProperty("group_kind", kind)     # ("kind" is the button style property in the QSS)
        lay.addWidget(rb)
        d = label(desc if (kind != KIND_SHORT or reader_ok) else
                  desc + " (Not available: the handwriting reader needs torch and transformers.)", "muted")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 6)
        lay.addWidget(d)
        if kind == KIND_SHORT and not reader_ok:
            rb.setEnabled(False)
        rb.setChecked(kind == pick and rb.isEnabled())
    if grp.checkedButton() is None:
        grp.buttons()[0].setChecked(True)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.button(QDialogButtonBox.StandardButton.Ok).setText("Continue")
    bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return grp.checkedButton().property("group_kind")

class ChoiceEditor(QDialog):
    """The choice boxes of a multiple choice question, on the blank exam page. Found automatically
    (bubbles with their labels, or the printed answers); the human adds, moves, resizes,
    deletes and renames them."""
    def __init__(self, parent, page_img, rect, problem: str, choices: List[dict] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Choices of {problem}")
        self.resize(1000, 640)
        self.page_img, self.rect = page_img, tuple(rect)
        x, y, w, h = self.rect
        pad = max(60, h // 2)
        H, W = page_img.shape[:2]
        self.ox, self.oy = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
        self.crop = page_img[self.oy:y1, self.ox:x1]
        self.choices: List[dict] = [dict(c) for c in (choices or [])]
        self.sel: Optional[int] = None

        lay = QVBoxLayout(self)
        self.lbl_info = label("")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)
        row = QHBoxLayout()
        self.viewer = PageViewer()
        row.addWidget(self.viewer, 1)
        side = QWidget(); sl = QVBoxLayout(side); sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(label("Choices", "sectionTitle"))
        self.list = QListWidget()
        sl.addWidget(self.list, 1)
        self.btn_find = button("Find again", "ghost", "Look for the choices on the blank page again "
                                                     "(replaces the boxes)")
        self.btn_delete = button("Delete", "danger", "Delete the selected box (Delete key)")
        sl.addWidget(self.btn_find); sl.addWidget(self.btn_delete)
        side.setFixedWidth(200)
        row.addWidget(side)
        lay.addLayout(row, 1)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept); self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)

        cv = self.viewer.canvas
        cv.rectDrawn.connect(self.add_box)
        cv.overlayClicked.connect(self.select)
        cv.overlayEdited.connect(self._edited)
        self.list.currentRowChanged.connect(lambda r: self.select(r if r >= 0 else None, from_list=True))
        self.list.itemChanged.connect(self._renamed)
        self.btn_find.clicked.connect(self.find)
        self.btn_delete.clicked.connect(self.delete_selected)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self, activated=self.delete_selected)
        self.viewer.set_image(self.crop)
        if not self.choices:
            self.find()
        else:
            self._render()

    # ---- coordinates: choices are kept in page pixels, the canvas shows the crop ----
    def _to_crop(self, r):
        return (r[0] - self.ox, r[1] - self.oy, r[2], r[3])

    def _to_page(self, r):
        return (int(r[0]) + self.ox, int(r[1]) + self.oy, int(r[2]), int(r[3]))

    def find(self):
        boxes = marks.detect_choices(self.page_img, self.rect)
        self.choices = [{"label": lab, "rect": b} for lab, b in zip(marks.default_labels(len(boxes)), boxes)]
        self.sel = None
        self._render()

    def _next_label(self) -> str:
        used = {c["label"] for c in self.choices}
        return next(l for l in marks.default_labels(len(self.choices) + 27) if l not in used)

    def add_box(self, rect):
        self.choices.append({"label": self._next_label(), "rect": self._to_page(rect)})
        self.sel = len(self.choices) - 1
        self._render()

    def select(self, key, from_list=False):
        self.sel = key if isinstance(key, int) and 0 <= key < len(self.choices) else None
        if not from_list:
            self.list.blockSignals(True)
            self.list.setCurrentRow(self.sel if self.sel is not None else -1)
            self.list.blockSignals(False)
        self._paint()

    def _edited(self, key, rect):
        if isinstance(key, int) and 0 <= key < len(self.choices):
            self.choices[key]["rect"] = self._to_page(rect)

    def _renamed(self, it):
        k = self.list.row(it)
        name = it.text().strip()
        if 0 <= k < len(self.choices) and name:
            self.choices[k]["label"] = name
        self._paint()

    def delete_selected(self):
        if self.sel is not None:
            del self.choices[self.sel]
            self.sel = None
            self._render()

    def _render(self):
        self.list.blockSignals(True)
        self.list.clear()
        for c in self.choices:
            it = QListWidgetItem(c["label"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            it.setToolTip("Double-click to rename")
            self.list.addItem(it)
        if self.sel is not None:
            self.list.setCurrentRow(self.sel)
        self.list.blockSignals(False)
        n = len(self.choices)
        self.lbl_info.setText(
            (f"<b>{n} choice{'s' if n != 1 else ''} found on the blank exam.</b> " if n else
             "<b>No choices found.</b> ")
            + "Each box should cover one printed choice (the bubble and its label, or the printed answer) "
              "and the space around it where students mark. Drag to draw a missing box; click a box to "
              "move or resize it; Delete removes it; double-click a name in the list to rename it.")
        self.bb.button(QDialogButtonBox.StandardButton.Save).setEnabled(n >= 2)
        self._paint()

    def _paint(self):
        cv = self.viewer.canvas
        ov = [Overlay(self._to_crop(self.rect), QColor(theme.MUTED), "", dashed=True)]
        for k, c in enumerate(self.choices):
            ov.append(Overlay(self._to_crop(c["rect"]), QColor(theme.ACCENT), c["label"], selected=k == self.sel,
                              key=k, editable=True))
        cv.active_key = self.sel
        cv.set_overlays(ov)
        self.btn_delete.setEnabled(self.sel is not None)

    def result_choices(self) -> List[dict]:
        """[{"label", "rect"}] in page pixels, labels made unique."""
        out, seen = [], set()
        for c in self.choices:
            lab = c["label"].strip() or "?"
            base, n = lab, 2
            while lab in seen:
                lab, n = f"{base}{n}", n + 1
            seen.add(lab)
            out.append({"label": lab, "rect": tuple(int(v) for v in c["rect"])})
        return out

def edit_choices(parent, page_img, rect, problem: str, choices: List[dict] = None) -> Optional[List[dict]]:
    """Show the choice editor; the boxes, or None (cancelled)."""
    dlg = ChoiceEditor(parent, page_img, rect, problem, choices)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.result_choices()
