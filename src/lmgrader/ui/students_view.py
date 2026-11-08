"""Students: the roster of the course picked in the sidebar."""
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QTableWidget, QTableWidgetItem, QMessageBox,
                             QLineEdit, QDialog, QFormLayout, QDialogButtonBox, QHeaderView, QAbstractItemView,
                             QInputDialog)
from PyQt6.QtCore import Qt
from ..db import (list_students, delete_student, add_student, current_course, course_name, list_courses,
                  move_students)
from ..models import Student
from .theme import button, card, label, setup_page
from .csv_dialogs import import_students

class StudentDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add student")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self.name = QLineEdit(); self.sid = QLineEdit(); self.email = QLineEdit()
        form.addRow("Name", self.name)
        form.addRow("Student ID", self.sid)
        form.addRow("Email", self.email)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)

class StudentsView(QWidget):
    def __init__(self):
        super().__init__()
        body, actions = setup_page(self, "Students",
                                   "The roster of the course picked in the sidebar: handwritten names on its "
                                   "scanned exams are matched against it.")
        self.btn_import = button("Import CSV…", tooltip="Any CSV with a header row: you pick the name, ID and "
                                                        "email columns (added to this course)")
        self.btn_add = button("Add student", "primary")
        self.btn_move = button("Move to course…", tooltip="Move the selected students to another course's roster "
                                                          "(their scanned tests keep their names)")
        self.btn_delete = button("Delete selected", "danger")
        for b in (self.btn_import, self.btn_move, self.btn_delete, self.btn_add):
            actions.addWidget(b)

        c, cl = card(margins=10)
        top = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search…"); self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.lbl_count = label("", "muted")
        top.addWidget(self.lbl_count)
        cl.addLayout(top)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Student ID", "Email"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        cl.addWidget(self.table, 1)
        body.addWidget(c, 1)

        self.btn_import.clicked.connect(self.import_csv)
        self.btn_add.clicked.connect(self.add_student)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_move.clicked.connect(self.move_selected)
        self.search.textChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        q = self.search.text().strip().lower()
        rows = [s for s in list_students()
                if not q or q in s.name.lower() or q in s.student_id.lower() or q in s.email.lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for s in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(s.name)
            name.setData(Qt.ItemDataRole.UserRole, s.id)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(s.student_id))
            self.table.setItem(r, 2, QTableWidgetItem(s.email))
        self.table.setSortingEnabled(True)
        self.lbl_count.setText(f"{len(rows)} students in {course_name(current_course())}")

    def import_csv(self):
        """Pick a CSV, then which columns hold the names (one or two columns), IDs and emails."""
        try:
            got = import_students(self, course_name(current_course()))
        except Exception as e:
            QMessageBox.critical(self, "Import students", f"Could not read the CSV:\n\n{e}")
            return
        if got is None:
            return
        n = self.add_new(got)
        QMessageBox.information(self, "Imported", f"Imported {n} new student{'s' if n != 1 else ''}"
                                + (f" ({len(got) - n} already in the roster)." if len(got) > n else "."))

    def add_new(self, students) -> int:
        """Add the students not in this course's roster yet (same name and ID); how many were added."""
        existing = {(s.name.lower(), s.student_id) for s in list_students()}
        added = 0
        for s in students:
            key = (s.name.lower(), s.student_id)
            if key not in existing:
                add_student(s)
                existing.add(key)
                added += 1
        self.refresh()
        return added

    def add_student(self):
        d = StudentDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.name.text().strip():
            return
        add_student(Student(name=d.name.text().strip(), student_id=d.sid.text().strip(), email=d.email.text().strip()))
        self.refresh()

    def _selected_ids(self):
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
                for r in sorted({i.row() for i in self.table.selectedIndexes()})]

    def move_selected(self):
        ids = self._selected_ids()
        others = [c for c in list_courses() if c["id"] != current_course()]
        if not ids:
            QMessageBox.information(self, "Move to course", "Select the students to move first.")
            return
        if not others:
            QMessageBox.information(self, "Move to course", "There is no other course yet: add one with "
                                                            "“Manage courses…” in the sidebar.")
            return
        name, ok = QInputDialog.getItem(self, "Move to course", f"Move {len(ids)} student(s) to:",
                                        [c["name"] for c in others], 0, False)
        if ok:
            self.move_to(ids, next(c["id"] for c in others if c["name"] == name))

    def move_to(self, ids, course_id):
        move_students(ids, course_id)
        self.refresh()

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows or QMessageBox.question(self, "Delete", f"Delete {len(rows)} student(s)?") != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            delete_student(self.table.item(r, 0).data(Qt.ItemDataRole.UserRole))
        self.refresh()

    def get_students(self):
        return list_students()
