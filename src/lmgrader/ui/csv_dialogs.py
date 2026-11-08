"""CSV column dialogs: which columns hold the names, IDs, emails (roster import) or the grade
(writing an exam's grades into the teacher's own CSV, e.g. a Canvas gradebook export)."""
import os
from typing import List, Optional

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QComboBox, QTableWidget, QTableWidgetItem,
                             QDialogButtonBox, QAbstractItemView, QHeaderView, QFileDialog, QMessageBox)

from .. import csvtable
from ..csvtable import Table, NAME_LAYOUTS, NAME_TWO_LAST_FIRST, two_columns
from ..models import Student
from .theme import label

PREVIEW_ROWS = 8

def _short_list(names: List[str], limit: int = 8) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")

class ColumnsDialog(QDialog):
    """Preview of the CSV + how names are written (one column "First Last" / "Last, First", or
    two columns: first name then last name, or last name then first name; the two pickers follow
    that order) + the ID column. Subclasses add their own columns."""
    def __init__(self, parent, table: Table, title: str, intro: str):
        super().__init__(parent)
        self.table = table
        self.setWindowTitle(title)
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        head = label(intro)
        head.setWordWrap(True)
        lay.addWidget(head)
        self.preview = QTableWidget(min(PREVIEW_ROWS, len(table.rows)), len(table.headers))
        self.preview.setHorizontalHeaderLabels([table.column_title(c) for c in range(len(table.headers))])
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for r, row in enumerate(table.rows[:PREVIEW_ROWS]):
            for c in range(len(table.headers)):
                self.preview.setItem(r, c, QTableWidgetItem(table.cell(row, c)))
        self.preview.setMaximumHeight(230)
        lay.addWidget(self.preview)
        lay.addWidget(label(f"{len(table.rows)} rows, {len(table.headers)} columns.", "muted"))

        self.form = QFormLayout()
        lay.addLayout(self.form)
        layout, col, col2 = csvtable.guess_name_columns(table)
        self.cb_layout = QComboBox()
        for k, v in NAME_LAYOUTS.items():
            self.cb_layout.addItem(v, k)
        self.cb_layout.setCurrentIndex(self.cb_layout.findData(layout))
        self.form.addRow("Names are written", self.cb_layout)
        self.cb_name = self._combo()
        self._set(self.cb_name, col)
        self.lbl_name = label("Name column")
        self.form.addRow(self.lbl_name, self.cb_name)
        self.cb_name2 = self._combo()         # the second name column of a two-column layout
        self._set(self.cb_name2, col2 if col2 >= 0 else min(col + 1, len(table.headers) - 1))
        self.lbl_name2 = label("Last name column")
        self.form.addRow(self.lbl_name2, self.cb_name2)
        self.cb_id = self._combo("(no ID column)")
        self._set(self.cb_id, csvtable.guess_column(table.headers, csvtable.ID_WORDS, exclude=[col, col2]))
        self.form.addRow("Student ID column", self.cb_id)

        self.lbl_example = label("", "muted")
        self.lbl_example.setWordWrap(True)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        for cb in (self.cb_layout, self.cb_name, self.cb_name2, self.cb_id):
            cb.currentIndexChanged.connect(lambda _: self._changed())

    def _finish(self):
        """Call at the end of a subclass __init__ (after its own rows)."""
        self.layout().addWidget(self.lbl_example)
        self.layout().addStretch()
        self.layout().addWidget(self.bb)
        self._changed()

    def _combo(self, none_text: str = None) -> QComboBox:
        cb = QComboBox()
        if none_text:
            cb.addItem(none_text, -1)
        for c in range(len(self.table.headers)):
            cb.addItem(self.table.column_title(c), c)
        return cb

    @staticmethod
    def _set(cb: QComboBox, col: int):
        i = cb.findData(col)
        cb.setCurrentIndex(i if i >= 0 else 0)

    @staticmethod
    def col(cb: QComboBox) -> int:
        v = cb.currentData()
        return -1 if v is None else int(v)

    @property
    def layout_kind(self) -> str:
        return self.cb_layout.currentData()

    @property
    def col2(self) -> int:
        """The second name column (two-column layouts), else -1."""
        return self.col(self.cb_name2) if two_columns(self.layout_kind) else -1

    def name_of(self, row) -> str:
        return csvtable.row_name(self.table, row, self.layout_kind, self.col(self.cb_name), self.col2)

    def _student_rows(self):
        return [r for r in self.table.rows if self.name_of(r) and not csvtable.is_points_row(self.name_of(r))]

    def problem(self) -> str:
        """Why OK can't be pressed yet ('' when it can)."""
        if two_columns(self.layout_kind) and self.col(self.cb_name) == self.col(self.cb_name2):
            return "The first and last name columns must be different."
        if not self._student_rows():
            return "No row has a name in that column."
        return ""

    def example(self) -> str:
        rows = self._student_rows()
        if not rows:
            return ""
        r = rows[0]
        sid = self.table.cell(r, self.col(self.cb_id)) if self.col(self.cb_id) >= 0 else ""
        return f"First student: {self.name_of(r)}" + (f" · ID {sid}" if sid else "")

    def _changed(self):
        two = two_columns(self.layout_kind)
        last_first = self.layout_kind == NAME_TWO_LAST_FIRST
        self.lbl_name.setText(("Last name column" if last_first else "First name column") if two else "Name column")
        self.lbl_name2.setText("First name column" if last_first else "Last name column")
        self.lbl_name2.setVisible(two)
        self.cb_name2.setVisible(two)
        bad = self.problem()
        self.lbl_example.setText(bad or self.example())
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not bad)

class ImportStudentsDialog(ColumnsDialog):
    """Students → Import CSV…: the name, ID and email columns of a roster (other columns, like
    grades of other assignments, are ignored)."""
    def __init__(self, parent, table: Table, course: str = ""):
        super().__init__(parent, table, "Import students",
                         f"Pick the columns of your roster{f' for {course}' if course else ''}. Other columns "
                         "(e.g. grades of other assignments) are ignored.")
        self.cb_email = self._combo("(no email column)")
        self._set(self.cb_email, csvtable.guess_column(table.headers, csvtable.EMAIL_WORDS,
                                                       exclude=[self.col(self.cb_name), self.col2,
                                                                self.col(self.cb_id)]))
        self.form.addRow("Email column", self.cb_email)
        self.cb_email.currentIndexChanged.connect(lambda _: self._changed())
        self._finish()

    def example(self) -> str:
        text = super().example()
        rows = self._student_rows()
        mail = self.table.cell(rows[0], self.col(self.cb_email)) if rows and self.col(self.cb_email) >= 0 else ""
        return text + (f" · {mail}" if mail else "") + (f" · {len(rows)} students" if rows else "")

    def students(self) -> List[Student]:
        from ..students.import_csv import students_from_table
        return students_from_table(self.table, self.layout_kind, self.col(self.cb_name), self.col2,
                                   self.col(self.cb_id), self.col(self.cb_email))

def import_students(parent, course: str = "") -> Optional[List[Student]]:
    """Pick a CSV and its columns: the students in it (None when cancelled)."""
    path, _ = QFileDialog.getOpenFileName(parent, "Import students CSV", "", "CSV (*.csv *.txt);;All files (*)")
    if not path:
        return None
    table = csvtable.read_table(path)
    dlg = ImportStudentsDialog(parent, table, course)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.students()

class GradesCsvDialog(ColumnsDialog):
    """Return → Write grades into a CSV…: the column the grade goes in, and how students are
    found (ID column first, then names)."""
    def __init__(self, parent, table: Table, exam: str):
        super().__init__(parent, table, f"Write the grades of {exam} into a CSV",
                         f"Pick the column for the grades of <b>{exam}</b> and the columns that say who each "
                         "row is. A row is matched by its student ID (or email) first, then by name. Every "
                         "other cell stays as it is, and a new file is saved.")
        self.cb_grade = QComboBox()
        self.cb_grade.addItem("— choose the column —", -1)
        for c in range(len(table.headers)):
            self.cb_grade.addItem(table.column_title(c), c)
        self._set(self.cb_grade, self.guess_grade_column(table, exam))
        self.form.insertRow(0, "Write the grade in", self.cb_grade)
        self.cb_grade.currentIndexChanged.connect(lambda _: self._changed())
        self._finish()

    @staticmethod
    def guess_grade_column(table: Table, exam: str) -> int:
        """A column whose header contains the exam name (Canvas: "Test 1 (123456)"), else -1."""
        key = csvtable.norm_name(exam)
        if not key:
            return -1
        return next((c for c, h in enumerate(table.headers) if key in csvtable.norm_name(h)), -1)

    def problem(self) -> str:
        if self.col(self.cb_grade) < 0:
            return "Choose the column the grade goes in."
        if self.col(self.cb_grade) in (self.col(self.cb_name), self.col2, self.col(self.cb_id)):
            return "The grade column can't be a name or ID column."
        return super().problem()

    def fill(self, items):
        from ..returns.grades_csv import fill_grades
        return fill_grades(self.table, items, self.col(self.cb_grade), self.layout_kind, self.col(self.cb_name),
                           self.col2, self.col(self.cb_id))

def grades_summary(exam: str, res, replaced: int, row_name) -> str:
    """What fill_grades did, for the confirmation before saving. row_name(row index) -> the
    name written in that CSV row."""
    n = len(res.written)
    lines = [f"Grades of {exam} for {n} student{'s' if n != 1 else ''} are ready to save."]
    if replaced:
        lines.append(f"{replaced} of these cells already had a different value; it is replaced.")
    close = [(it, row_name(r)) for r, it, how in res.written if how == "close name"]
    if close:
        lines.append("Matched by a similar name (check these): " + _short_list(
            [f"{it.student.name} ↔ “{name}”" for it, name in close]))
    if res.duplicates:
        lines.append(f"Left empty, {len(res.duplicates)} student(s) have two graded tests: "
                     + _short_list(res.duplicates))
    if res.not_in_csv:
        lines.append(f"{len(res.not_in_csv)} graded test(s) have no row in the CSV: " + _short_list(
            [it.student.name if it.student else f"unassigned test #{it.sub.id}" for it in res.not_in_csv]))
    if res.no_test:
        lines.append(f"{len(res.no_test)} row(s) of the CSV have no graded test and are left as they were: "
                     + _short_list(res.no_test))
    return "\n\n".join(lines)

def write_grades_csv(parent, exam: str, items) -> bool:
    """Pick the teacher's CSV, the grade column and the name/ID columns, fill in each student's
    total and save it as a new file. True when a file was written."""
    path, _ = QFileDialog.getOpenFileName(parent, f"CSV to write the grades of {exam} into", "",
                                          "CSV (*.csv *.txt);;All files (*)")
    if not path:
        return False
    try:
        table = csvtable.read_table(path)
    except (OSError, ValueError) as e:
        QMessageBox.critical(parent, "Grades CSV", f"Could not read the CSV:\n\n{e}")
        return False
    dlg = GradesCsvDialog(parent, table, exam)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    col = dlg.col(dlg.cb_grade)
    before = [table.cell(row, col) for row in table.rows]
    res = dlg.fill(items)
    if not res.written:
        QMessageBox.warning(parent, "Grades CSV", "No row of the CSV matched a graded test of "
                                                  f"{exam}. Check the name and ID columns.")
        return False
    replaced = sum(1 for r, _, _ in res.written if before[r] and before[r] != table.cell(table.rows[r], col))
    yes = QMessageBox.StandardButton.Yes
    text = grades_summary(exam, res, replaced, lambda r: dlg.name_of(table.rows[r]))
    if QMessageBox.question(parent, "Grades CSV", text + "\n\nSave the CSV?",
                            yes | QMessageBox.StandardButton.Cancel) != yes:
        return False
    stem, ext = os.path.splitext(path)
    out, _ = QFileDialog.getSaveFileName(parent, "Save the CSV with the grades",
                                         f"{stem} - {exam}{ext or '.csv'}", "CSV (*.csv)")
    if not out:
        return False
    try:
        csvtable.write_table(out, table)
    except OSError as e:
        QMessageBox.critical(parent, "Grades CSV", f"Could not save the CSV:\n\n{e}")
        return False
    QMessageBox.information(parent, "Grades CSV", f"Saved the grades of {len(res.written)} students to\n{out}")
    return True
