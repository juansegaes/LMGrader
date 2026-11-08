"""CSV reading/writing: column guessing, roster import, and writing grades into a gradebook."""
import pytest

from lmgrader.csvtable import (read_table, write_table, guess_name_columns, NAME_FIRST_LAST,
                               NAME_LAST_FIRST, NAME_TWO_COLUMNS)
from lmgrader.models import Student, Submission
from lmgrader.returns.items import ReturnItem
from lmgrader.returns.grades_csv import fill_grades
from lmgrader.students.import_csv import import_csv, students_from_table


def _write(tmp_path, text, name="gb.csv", encoding="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(encoding))
    return str(p)


def _item(test_id, student, total=8.0):
    return ReturnItem(sub=Submission(id=test_id, student_id=student.id if student else None),
                      student=student, exam="T1", version="A", total=total, max=10,
                      needs_review=False)


# ---------- read_table / write_table ----------

def test_read_comma_file(tmp_path):
    t = read_table(_write(tmp_path, "name,id\nAna Lopez,100\nBen Cole,101\n"))
    assert t.headers == ["name", "id"]
    assert t.delimiter == ","
    assert t.rows == [["Ana Lopez", "100"], ["Ben Cole", "101"]]
    assert t.bom is False


def test_read_semicolon_file(tmp_path):
    t = read_table(_write(tmp_path, "Name;ID;Grade\nAna Lopez;100;9\nBen Cole;101;7\nCy Dunn;102;5\n"))
    assert t.delimiter == ";"
    assert t.headers == ["Name", "ID", "Grade"]
    assert t.rows[1] == ["Ben Cole", "101", "7"]


def test_utf8_bom_round_trip(tmp_path):
    src = _write(tmp_path, "name,grade\nAna Lopez,9\n", name="bom.csv", encoding="utf-8-sig")
    t = read_table(src)
    assert t.bom is True
    assert t.headers == ["name", "grade"]
    out = str(tmp_path / "out.csv")
    write_table(out, t)
    raw = open(out, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")
    t2 = read_table(out)
    assert t2.bom is True and t2.headers == t.headers and t2.rows == t.rows


def test_cp1252_file_decodes(tmp_path):
    src = _write(tmp_path, "name,grade\nJosé Pérez,8\n", name="cp.csv", encoding="cp1252")
    t = read_table(src)
    assert t.encoding == "cp1252"
    assert t.rows == [["José Pérez", "8"]]


def test_write_table_keeps_other_cells_and_delimiter(tmp_path):
    src = _write(tmp_path, "Name;HW1;HW2\nJosé;5;6\nBen;7;8\n", name="semi.csv", encoding="cp1252")
    t = read_table(src)
    assert t.encoding == "cp1252"
    out = str(tmp_path / "out.csv")
    write_table(out, t)
    assert open(out, "rb").read().decode("cp1252").startswith("Name;HW1;HW2")
    t2 = read_table(out)
    assert t2.delimiter == ";"
    assert t2.headers == ["Name", "HW1", "HW2"]
    assert t2.rows == [["José", "5", "6"], ["Ben", "7", "8"]]


# ---------- guess_name_columns ----------

def test_guess_single_name_column_first_last(tmp_path):
    t = read_table(_write(tmp_path, "name,id\nAna Lopez,100\nBen Cole,101\n"))
    assert guess_name_columns(t) == (NAME_FIRST_LAST, 0, -1)


def test_guess_student_column_last_first(tmp_path):
    t = read_table(_write(tmp_path, 'Student,ID,HW1\n"Lopez, Ana",100,9\n"Cole, Ben",101,7\n"Dunn, Cy",102,5\n'))
    layout, col, col2 = guess_name_columns(t)
    assert (layout, col, col2) == (NAME_LAST_FIRST, 0, -1)


def test_guess_first_and_last_name_columns(tmp_path):
    t = read_table(_write(tmp_path, "First Name,Last Name,Email\nAna,Lopez,a@x.edu\n"))
    assert guess_name_columns(t) == (NAME_TWO_COLUMNS, 0, 1)


def test_last_name_column_before_first_name_column(tmp_path):
    from lmgrader.csvtable import NAME_TWO_LAST_FIRST, row_name
    from lmgrader.students.import_csv import import_csv
    p = _write(tmp_path, "ID,Last Name,First Name\n7,Lopez,Ana\n8,Cole,Ben\n")
    t = read_table(p)
    assert guess_name_columns(t) == (NAME_TWO_LAST_FIRST, 1, 2)          # in the file's order
    assert row_name(t, t.rows[0], NAME_TWO_LAST_FIRST, 1, 2) == "Ana Lopez"
    assert row_name(t, t.rows[0], NAME_TWO_COLUMNS, 1, 2) == "Lopez Ana"  # the order is the layout's
    assert [(s.name, s.student_id) for s in import_csv(p)] == [("Ana Lopez", "7"), ("Ben Cole", "8")]


# ---------- students_from_table / import_csv ----------

GRADEBOOK = (
    "Student,ID,SIS User ID,Email,HW1,HW2\n"
    "Points Possible,,,,10,10\n"
    '"Lopez, Ana",1001,ana1,ana@x.edu,9,8\n'
    ",,,,,\n"
    '"Cole, Ben",1002,ben2,ben@x.edu,7,6\n'
)


def test_students_from_gradebook_skips_points_and_empty_rows(tmp_path):
    t = read_table(_write(tmp_path, GRADEBOOK))
    layout, col, col2 = guess_name_columns(t)
    assert layout == NAME_LAST_FIRST
    students = students_from_table(t, layout, col, col2, id_col=2, email_col=3)
    assert [s.name for s in students] == ["Ana Lopez", "Ben Cole"]
    assert students[0].student_id == "ana1"
    assert students[0].email == "ana@x.edu"


def test_gradebook_id_and_email_columns(tmp_path):
    # the SIS User ID column is the student id, not the Canvas "ID" column
    t = read_table(_write(tmp_path, GRADEBOOK))
    students = students_from_table(t, NAME_LAST_FIRST, 0, -1, id_col=2, email_col=3)
    assert students[0].student_id == "ana1"
    assert students[1].student_id == "ben2"
    assert students[1].email == "ben@x.edu"


def test_students_two_column_layout(tmp_path):
    t = read_table(_write(tmp_path, "First Name,Last Name,Email\nAna,Lopez,a@x.edu\nBen,Cole,b@x.edu\n"))
    layout, col, col2 = guess_name_columns(t)
    students = students_from_table(t, layout, col, col2, email_col=2)
    assert [(s.name, s.email) for s in students] == [("Ana Lopez", "a@x.edu"), ("Ben Cole", "b@x.edu")]


def test_import_csv_old_style_last_first(tmp_path):
    src = _write(tmp_path, 'name,student_id,email\n"Lopez, Ana",100,ana@x.edu\n"Cole, Ben",101,ben@x.edu\n')
    students = import_csv(src)
    assert [(s.name, s.student_id, s.email) for s in students] == [
        ("Ana Lopez", "100", "ana@x.edu"),
        ("Ben Cole", "101", "ben@x.edu"),
    ]


def test_import_csv_old_style_plain(tmp_path):
    src = _write(tmp_path, "name,student_id,email\nAna Lopez,100,ana@x.edu\nBen Cole,101,ben@x.edu\n")
    students = import_csv(src)
    assert [(s.name, s.student_id, s.email) for s in students] == [
        ("Ana Lopez", "100", "ana@x.edu"),
        ("Ben Cole", "101", "ben@x.edu"),
    ]


# ---------- fill_grades ----------

ANA = Student(id=1, name="Ana Lopez", student_id="100", email="ana@x.edu")
BEN = Student(id=2, name="Ben Cole", student_id="101", email="ben@x.edu")
CY = Student(id=3, name="Cy Dunn", student_id="102", email="cy@x.edu")
NORA = Student(id=4, name="Nora Vance", student_id="104", email="nora@x.edu")


def _gradebook_table(tmp_path, body, encoding="utf-8"):
    header = "Student,ID,Email,HW1,Final\n"
    t = read_table(_write(tmp_path, header + body, name="grades.csv", encoding=encoding))
    return t


def test_fill_match_by_id(tmp_path):
    t = _gradebook_table(tmp_path, "Zzz Other,101,,5,\n")
    items = [_item(11, BEN, total=8.5)]
    res = fill_grades(t, items, grade_col=4, layout=NAME_FIRST_LAST, name_col=0, id_col=1)
    assert [(r, how) for r, _, how in res.written] == [(0, "id")]
    assert t.rows[0][4] == "8.5"


def test_fill_match_by_last_first_name(tmp_path):
    # "Last, First" in the file, "First Last" on the roster
    t = _gradebook_table(tmp_path, '"Lopez, Ana",,,,\n')
    res = fill_grades(t, [_item(11, ANA, total=9)], grade_col=4, layout=NAME_LAST_FIRST, name_col=0)
    assert [how for _, _, how in res.written] == ["name"]
    assert t.rows[0][4] == "9"


def test_fill_match_reversed_word_order(tmp_path):
    t = _gradebook_table(tmp_path, "Cole Ben,,,,\n")
    res = fill_grades(t, [_item(12, BEN, total=7)], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert [how for _, _, how in res.written] == ["name"]
    assert t.rows[0][4] == "7"


def test_fill_close_name_typo(tmp_path):
    t = _gradebook_table(tmp_path, "Anna Lopez,,,,\nCy Dunn,,,,\n")
    res = fill_grades(t, [_item(11, ANA, total=6), _item(13, CY, total=4)],
                      grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    hows = {r: how for r, _, how in res.written}
    assert hows == {0: "close name", 1: "name"}
    assert t.rows[0][4] == "6"
    assert t.rows[1][4] == "4"


def test_exact_name_not_stolen_by_fuzzy_match(tmp_path):
    # "Anna Lopez" (earlier row) is close to Ana Lopez, but the exact row is matched in the name pass
    t = _gradebook_table(tmp_path, "Anna Lopez,,,,\nAna Lopez,,,,\n")
    res = fill_grades(t, [_item(11, ANA, total=9)], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert [(r, how) for r, _, how in res.written] == [(1, "name")]
    assert t.rows[0][4] == ""
    assert t.rows[1][4] == "9"
    assert res.no_test == ["Anna Lopez"]


def test_unmatched_row_left_unchanged_and_listed(tmp_path):
    t = _gradebook_table(tmp_path, "Zed Quux,,,,\nAna Lopez,,,,\n")
    res = fill_grades(t, [_item(11, ANA, total=9)], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert t.rows[0][4] == ""
    assert res.no_test == ["Zed Quux"]


def test_test_whose_student_is_not_in_csv(tmp_path):
    t = _gradebook_table(tmp_path, "Ana Lopez,,,,\n")
    orphan = _item(14, NORA, total=5)
    res = fill_grades(t, [_item(11, ANA, total=9), orphan], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert res.not_in_csv == [orphan]
    assert res.no_test == []


def test_test_without_student_goes_to_not_in_csv(tmp_path):
    t = _gradebook_table(tmp_path, "Ana Lopez,,,,\n")
    anon = _item(15, None, total=3)
    res = fill_grades(t, [anon], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert res.not_in_csv == [anon]
    assert res.written == []
    assert t.rows[0][4] == ""


def test_student_with_two_tests_is_duplicate_and_unchanged(tmp_path):
    t = _gradebook_table(tmp_path, "Ana Lopez,,,,\nBen Cole,,,,\n")
    items = [_item(11, ANA, total=9), _item(12, ANA, total=4), _item(13, BEN, total=7)]
    res = fill_grades(t, items, grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert res.duplicates == ["Ana Lopez"]
    assert t.rows[0][4] == ""           # the duplicate's row is untouched
    assert t.rows[1][4] == "7"
    assert "Ana Lopez" not in res.no_test
    assert [r for r, _, _ in res.written] == [1]


def test_points_possible_row_untouched(tmp_path):
    t = _gradebook_table(tmp_path, "Points Possible,,,10,10\nAna Lopez,,,,\n")
    res = fill_grades(t, [_item(11, ANA, total=9)], grade_col=4, layout=NAME_FIRST_LAST, name_col=0)
    assert t.rows[0] == ["Points Possible", "", "", "10", "10"]
    assert t.rows[1][4] == "9"
    assert res.no_test == []


def test_other_columns_unchanged_after_write(tmp_path):
    src = _write(tmp_path, 'Student,ID,Email,HW1,Final\n"Lopez, Ana",100,ana@x.edu,9,\n'
                           'Points Possible,,,10,10\n"Cole, Ben",101,ben@x.edu,7,\n')
    t = read_table(src)
    before = [list(r) for r in t.rows]
    fill_grades(t, [_item(11, ANA, total=8.5), _item(12, BEN, total=6)],
                grade_col=4, layout=NAME_LAST_FIRST, name_col=0, id_col=1)
    out = str(tmp_path / "out.csv")
    write_table(out, t)
    t2 = read_table(out)
    assert t2.headers == ["Student", "ID", "Email", "HW1", "Final"]
    assert t2.rows[0] == ["Lopez, Ana", "100", "ana@x.edu", "9", "8.5"]
    assert t2.rows[1] == ["Points Possible", "", "", "10", "10"]
    assert t2.rows[2] == ["Cole, Ben", "101", "ben@x.edu", "7", "6"]
    assert t2.delimiter == "," and t2.bom is False
    for r_before, r_after in zip(before, t.rows):
        assert r_before[:4] == r_after[:4]


def test_bad_grade_column_raises(tmp_path):
    t = _gradebook_table(tmp_path, "Ana Lopez,,,,\n")
    with pytest.raises(ValueError):
        fill_grades(t, [_item(11, ANA)], grade_col=9, layout=NAME_FIRST_LAST, name_col=0)
    with pytest.raises(ValueError):
        fill_grades(t, [_item(11, ANA)], grade_col=-1, layout=NAME_FIRST_LAST, name_col=0)
