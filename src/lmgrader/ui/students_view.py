"""Students management view."""
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox,
                             QLineEdit, QDialog, QFormLayout, QDialogButtonBox, QHeaderView, QAbstractItemView)
from PyQt6.QtCore import Qt
from ..db import list_students, delete_student, add_student
from ..students import import_csv
from ..models import Student
from .theme import button, card, label, setup_page

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
                                   "The roster used to match handwritten names on scanned exams.")
        self.btn_import = button("Import CSV…", tooltip="Columns: name, student_id, email")
        self.btn_add = button("Add student", "primary")
        self.btn_delete = button("Delete selected", "danger")
        for b in (self.btn_import, self.btn_delete, self.btn_add):
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
        self.lbl_count.setText(f"{len(rows)} students")

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Students CSV", "", "CSV (*.csv)")
        if not path:
            return
        try:
            existing = {(s.name.lower(), s.student_id) for s in list_students()}
            students = [s for s in import_csv(path) if (s.name.lower(), s.student_id) not in existing]
            for s in students:
                add_student(s)
            QMessageBox.information(self, "Imported", f"Imported {len(students)} new students.")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def add_student(self):
        d = StudentDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.name.text().strip():
            return
        add_student(Student(name=d.name.text().strip(), student_id=d.sid.text().strip(), email=d.email.text().strip()))
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
