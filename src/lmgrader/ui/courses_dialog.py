"""Manage courses: add, rename and delete (empty) courses. Each course has its own roster and exams."""
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QInputDialog,
                             QMessageBox, QDialogButtonBox)
from PyQt6.QtCore import Qt

from ..db import list_courses, add_course, rename_course, delete_course
from .theme import button, label

class CoursesDialog(QDialog):
    def __init__(self, parent=None, current: int = None):
        super().__init__(parent)
        self.setWindowTitle("Courses")
        self.resize(520, 420)
        self.selected = current          # highlighted course
        self.created = None              # the course added last (the main window opens it)
        lay = QVBoxLayout(self)
        info = label("Each course has its own roster (Students) and its own exams. Pick the course to work on "
                     "in the sidebar. A course can be deleted once it has no students and no exams (deleting an "
                     "exam deletes its scans); move exams with “Copy / move to course…” in Exams & Templates "
                     "and students with “Move to course…” in Students.", "muted")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        self.list = QListWidget()
        row.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.btn_new = button("New course…", "primary")
        self.btn_rename = button("Rename…")
        self.btn_delete = button("Delete", "danger", "Only a course without students or exams can be deleted")
        for b in (self.btn_new, self.btn_rename, self.btn_delete):
            side.addWidget(b)
        side.addStretch()
        row.addLayout(side)
        lay.addLayout(row, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        lay.addWidget(bb)
        self.btn_new.clicked.connect(self._ask_new)
        self.btn_rename.clicked.connect(self._ask_rename)
        self.btn_delete.clicked.connect(self._ask_delete)
        self.list.currentRowChanged.connect(self._picked)
        self.list.itemDoubleClicked.connect(lambda _: self._ask_rename())
        self.refresh()

    def refresh(self):
        self.list.blockSignals(True)     # the buttons are set once the whole list is there
        self.list.clear()
        for c in list_courses():
            tests = f"{c['tests']} scanned tests" + (" (of deleted exams)" if c["tests"] and not c["exams"] else "")
            it = QListWidgetItem(f"{c['name']}   ·   {c['students']} students, {c['exams']} exams, {tests}")
            it.setData(Qt.ItemDataRole.UserRole, c["id"])
            it.setData(Qt.ItemDataRole.UserRole + 1, c["name"])
            self.list.addItem(it)
            if c["id"] == self.selected:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._picked(self.list.currentRow())

    def _picked(self, row):
        it = self.list.item(row)
        if it is not None:
            self.selected = it.data(Qt.ItemDataRole.UserRole)
        self.btn_rename.setEnabled(it is not None)
        self.btn_delete.setEnabled(it is not None and self.list.count() > 1)

    def _current_id(self):
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    # ---- actions (also called directly by tests) ----
    def new_course(self, name: str) -> int:
        cid = add_course(name)
        self.selected = self.created = cid
        self.refresh()
        return cid

    def rename(self, cid: int, name: str):
        rename_course(cid, name)
        self.refresh()

    def delete(self, cid: int):
        delete_course(cid)
        if self.selected == cid:
            self.selected = None
        self.refresh()

    def _ask_new(self):
        name, ok = QInputDialog.getText(self, "New course", "Course name (e.g. MATH 2164-007 Fall 2026):")
        if ok and name.strip():
            self._try(lambda: self.new_course(name))

    def _ask_rename(self):
        cid, it = self._current_id(), self.list.currentItem()
        if cid is None:
            return
        old = it.data(Qt.ItemDataRole.UserRole + 1)
        name, ok = QInputDialog.getText(self, "Rename course", "Course name:", text=old)
        if ok and name.strip() and name.strip() != old:
            self._try(lambda: self.rename(cid, name))

    def _ask_delete(self):
        cid = self._current_id()
        info = next((c for c in list_courses() if c["id"] == cid), None)
        if info is None:
            return
        if info["students"] or info["exams"]:
            self._try(lambda: self.delete(cid))          # refused, with the reason
            return
        Yes, Cancel = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel
        n = info["tests"]
        if n:     # tests of exams deleted before (deleting an exam now deletes its tests)
            ok = QMessageBox.warning(self, "Delete course",
                                     f"Delete “{info['name']}”? Its {n} scanned test{'s' if n != 1 else ''} of "
                                     f"deleted exams (no tab shows them) and their grades are deleted with it. "
                                     f"This can't be undone.", Yes | Cancel, Cancel) == Yes
        else:
            ok = QMessageBox.question(self, "Delete course", f"Delete “{info['name']}”?") == Yes
        if ok:
            self._try(lambda: self.delete(cid))

    def _try(self, fn):
        try:
            fn()
        except ValueError as e:
            QMessageBox.warning(self, "Courses", str(e))
