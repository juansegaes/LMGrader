"""CSV column dialogs: roster import with picked columns, grades written into the teacher's CSV."""
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QMessageBox

from conftest import SCREENSHOT_DIR
from lmgrader import csvtable, db
from lmgrader.models import Student, Submission
from lmgrader.returns.items import ReturnItem

GRADEBOOK = ('Student,ID,SIS User ID,SIS Login ID,Section,Email,HW1 (1),Test 1 (2)\r\n'
             '    Points Possible,,,,,,10,100\r\n'
             '"Adames, Mel",101,800001,madames,S1,madames@charlotte.edu,9,\r\n'
             '"Aguegue, Grace",102,800002,gaguegue,S1,,8,\r\n'
             '"Absent, Al",103,800003,aabsent,S1,,7,\r\n')

def _gradebook(tmp_path):
    p = tmp_path / "gradebook.csv"
    p.write_bytes(GRADEBOOK.encode("utf-8"))
    return str(p)

def test_import_dialog_picks_columns(qapp, tmp_path):
    from lmgrader.ui.csv_dialogs import ImportStudentsDialog
    dlg = ImportStudentsDialog(None, csvtable.read_table(_gradebook(tmp_path)), "MATH 101")
    # guessed: "Last, First" in the first column, SIS User ID, Email; grade columns ignored
    assert dlg.layout_kind == csvtable.NAME_LAST_FIRST and dlg.col(dlg.cb_name) == 0
    assert dlg.col(dlg.cb_id) == 2 and dlg.col(dlg.cb_email) == 5
    assert not dlg.cb_name2.isVisibleTo(dlg)
    assert dlg.lbl_example.text().startswith("First student: Mel Adames · ID 800001")
    got = dlg.students()
    assert [(s.name, s.student_id, s.email) for s in got] == [
        ("Mel Adames", "800001", "madames@charlotte.edu"), ("Grace Aguegue", "800002", ""), ("Al Absent", "800003", "")]
    dlg.cb_id.setCurrentIndex(dlg.cb_id.findData(3))           # the login instead
    assert dlg.students()[0].student_id == "madames"
    dlg.cb_id.setCurrentIndex(dlg.cb_id.findData(-1))          # no ID column
    assert dlg.students()[0].student_id == ""
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    dlg.show(); qapp.processEvents()
    dlg.grab().save(str(SCREENSHOT_DIR / "import_students.png"))
    dlg.close()

def test_import_dialog_two_name_columns(qapp, tmp_path):
    from lmgrader.ui.csv_dialogs import ImportStudentsDialog
    p = tmp_path / "r.csv"
    p.write_text("Student Number;First Name;Last Name;HW\n1001;Ana;Torres;5\n1002;John;Smith;\n", encoding="utf-8")
    dlg = ImportStudentsDialog(None, csvtable.read_table(str(p)))
    assert dlg.layout_kind == csvtable.NAME_TWO_COLUMNS and (dlg.col(dlg.cb_name), dlg.col2) == (1, 2)
    assert dlg.cb_name2.isVisibleTo(dlg) and dlg.lbl_name.text() == "First name column"
    assert [(s.name, s.student_id) for s in dlg.students()] == [("Ana Torres", "1001"), ("John Smith", "1002")]
    ok = dlg.bb.button(QDialogButtonBox.StandardButton.Ok)
    dlg.cb_name2.setCurrentIndex(dlg.cb_name2.findData(1))       # same column twice: refused
    assert not ok.isEnabled() and "different" in dlg.lbl_example.text()
    dlg.cb_layout.setCurrentIndex(dlg.cb_layout.findData(csvtable.NAME_FIRST_LAST))
    assert ok.isEnabled() and not dlg.cb_name2.isVisibleTo(dlg) and dlg.students()[0].name == "Ana"

def test_import_dialog_last_name_then_first_name(qapp, tmp_path):
    from lmgrader.ui.csv_dialogs import ImportStudentsDialog
    # no first/last headers: the pickers take the name column and the one after it, in the
    # order the layout says
    p = tmp_path / "r2.csv"
    p.write_text("Surname,Given,Mail\nTorres,Ana,ana@x.edu\nSmith,John,\n", encoding="utf-8")
    dlg = ImportStudentsDialog(None, csvtable.read_table(str(p)))
    assert dlg.layout_kind == csvtable.NAME_FIRST_LAST
    assert [dlg.cb_layout.itemData(i) for i in range(dlg.cb_layout.count())] == [
        csvtable.NAME_FIRST_LAST, csvtable.NAME_LAST_FIRST, csvtable.NAME_TWO_COLUMNS, csvtable.NAME_TWO_LAST_FIRST]
    dlg.cb_layout.setCurrentIndex(dlg.cb_layout.findData(csvtable.NAME_TWO_LAST_FIRST))
    assert (dlg.lbl_name.text(), dlg.lbl_name2.text()) == ("Last name column", "First name column")
    assert (dlg.col(dlg.cb_name), dlg.col2) == (0, 1) and dlg.cb_name2.isVisibleTo(dlg)
    assert dlg.lbl_example.text().startswith("First student: Ana Torres")
    assert [(s.name, s.email) for s in dlg.students()] == [("Ana Torres", "ana@x.edu"), ("John Smith", "")]
    dlg.cb_layout.setCurrentIndex(dlg.cb_layout.findData(csvtable.NAME_TWO_COLUMNS))
    assert (dlg.lbl_name.text(), dlg.lbl_name2.text()) == ("First name column", "Last name column")
    assert dlg.students()[0].name == "Torres Ana"

def test_students_view_import_adds_only_new(qapp, tmp_path, monkeypatch):
    from lmgrader.ui import students_view
    v = students_view.StudentsView()
    got = [Student(name="Csv Person One", student_id="cp1"), Student(name="Csv Person Two", student_id="cp2")]
    monkeypatch.setattr(students_view, "import_students", lambda *a, **k: list(got))
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    v.import_csv()
    v.import_csv()
    assert shown == ["Imported 2 new students.", "Imported 0 new students (2 already in the roster)."]
    assert sum(s.name.startswith("Csv Person") for s in db.list_students()) == 2

def _items():
    mel = Student(id=9001, name="Mel Adames", student_id="800001")
    grace = Student(id=9002, name="Grace Aguege", student_id="")         # typo, no ID: close name
    other = Student(id=9003, name="Not In Csv", student_id="x")
    return [ReturnItem(sub=Submission(id=n, student_id=s.id), student=s, exam="Test 1", version="A",
                       total=t, max=100, needs_review=False)
            for n, (s, t) in enumerate([(mel, 91.5), (grace, 78), (other, 50)], start=1)]

def test_write_grades_into_the_teachers_csv(qapp, tmp_path, monkeypatch):
    from lmgrader.ui import csv_dialogs
    src = _gradebook(tmp_path)
    out = str(tmp_path / "out.csv")
    dlg = csv_dialogs.GradesCsvDialog(None, csvtable.read_table(src), "Test 1")
    assert dlg.col(dlg.cb_grade) == 7 and dlg.col(dlg.cb_id) == 2      # "Test 1 (2)" found by the exam name
    dlg.cb_grade.setCurrentIndex(0)
    assert not dlg.bb.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    dlg.cb_grade.setCurrentIndex(dlg.cb_grade.findData(7))
    dlg.show(); qapp.processEvents()
    dlg.grab().save(str(SCREENSHOT_DIR / "grades_csv.png"))
    dlg.close()

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (src, ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (out, ""))
    monkeypatch.setattr(csv_dialogs.GradesCsvDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    asked, told = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    assert csv_dialogs.write_grades_csv(None, "Test 1", _items())
    text = asked[0]
    assert text.startswith("Grades of Test 1 for 2 students are ready to save.")
    assert "Grace Aguege ↔ “Grace Aguegue”" in text and "Not In Csv" in text and "Al Absent" in text
    rows = csvtable.read_table(out).rows
    assert rows[0] == ["    Points Possible", "", "", "", "", "", "10", "100"]          # untouched
    assert rows[1][6:] == ["9", "91.5"] and rows[2][6:] == ["8", "78"] and rows[3][6:] == ["7", ""]
    assert told and out in told[0]
    assert open(src, encoding="utf-8").read() == GRADEBOOK.replace("\r\n", "\n")       # the original is kept
