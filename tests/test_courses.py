"""Courses: each has its own roster, exams (names unique within a course), scans and exam
settings; every tab shows the course picked in the sidebar."""
import time

import pytest

from conftest import make_page
from lmgrader import db
from lmgrader.config import UI_CONFIG
from lmgrader.models import Student, Submission, SUB_NEW
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist


@pytest.fixture
def course_b(world):
    """A second, empty course. The current course is restored and B removed afterwards."""
    home = db.current_course()
    b = db.add_course(f"Course B {time.time_ns()}")
    yield home, b
    db.set_current_course(home)
    for t in db.list_templates(b):
        db.delete_template(t.id)
    for s in db.list_submissions_by_status(course_id=b):
        db.delete_submission(s.id)
    for s in db.list_students(b):
        db.delete_student(s.id)
    db.delete_course(b)


def _exam(data_dir, name, course_id=None, seed=60):
    p = str(data_dir / f"{name.replace(' ', '_')}_{time.time_ns()}.png")
    save_image(p, make_page(seed, name.upper()))
    t = create_template_from_image(f"{name} vA", p, name, "A")
    t.course_id = course_id
    add_region(t, "Q1", "g", (100, 300, 300, 150), 2, rubric="- [2 pt] right")
    t.id = persist(t)
    return t, p


def test_existing_data_went_into_one_course(world):
    home = db.current_course()
    assert db.course_name(home) == db.DEFAULT_COURSE
    old = next(t for t in db.list_templates() if t.name == "Old quiz")        # saved before courses
    assert old.course_id == home
    assert {"Alice Johnson", "Bob Smith"} <= {s.name for s in db.list_students()}
    assert all(s.course_id is not None for s in db.list_students(all_courses=True))
    assert all(t.course_id is not None for t in db.list_templates(all_courses=True))


def test_each_course_has_its_own_roster_exams_scans_and_settings(course_b, data_dir):
    from lmgrader.grading.grader import Grader
    home, b = course_b
    db.set_exam_instructions("Midterm", "home rules")
    try:
        db.set_current_course(b)
        assert db.list_exams() == [] and db.list_students() == []
        t, p = _exam(data_dir, "Midterm")                       # same name as the home course's exam
        db.add_student(Student(name="Zoe Course-B"))
        db.set_exam_instructions("Midterm", "B rules")
        db.set_exam_strictness("Midterm", 6)
        sub = Submission(exam_name="Midterm", status=SUB_NEW, scan_pages=[p])
        db.add_submission(sub)
        assert [v.id for v in db.list_versions("Midterm")] == [t.id] and t.course_id == b
        assert [s.name for s in db.list_students()] == ["Zoe Course-B"]
        assert [s.id for s in db.list_submissions_by_status("Midterm")] == [sub.id]
        assert db.get_exam_instructions("Midterm") == "B rules"

        db.set_current_course(home)
        home_versions = db.list_versions("Midterm")
        assert len(home_versions) == 2 and t.id not in [v.id for v in home_versions]
        assert sub.id not in [s.id for s in db.list_submissions_by_status("Midterm")]
        assert "Zoe Course-B" not in {s.name for s in db.list_students()}
        assert db.get_exam_instructions("Midterm") == "home rules"
        assert (db.get_exam_instructions("Midterm", b), db.get_exam_strictness("Midterm", b)) == ("B rules", 6)
        # grading (a background job) uses the course of the exam it grades, whatever is shown
        assert Grader._context(db.load_template(t.id))["instructions"] == "B rules"
        assert Grader._context(home_versions[0])["instructions"] == "home rules"
        assert sub.id in [s.id for s in db.list_submissions_by_status(all_courses=True)]
    finally:
        db.set_exam_instructions("Midterm", "", home)


def test_copy_and_move_exams_between_courses(course_b, world, data_dir):
    home, b = course_b
    ta = db.load_template(world["ta_id"])
    uid = ta.regions[0].uid
    db.set_hand_setup(ta.id, uid, kind="choice",
                      choices=[{"label": "A", "rect": (1, 2, 30, 20)}, {"label": "B", "rect": (1, 40, 30, 20)}])
    db.set_exam_strictness("Midterm", 7)
    try:
        ids = db.copy_exam("Midterm", home, b)
        copies = {v.version_label: v for v in db.list_versions("Midterm", b)}
        assert sorted(copies) == ["A", "B"] and sorted(ids) == sorted(v.id for v in copies.values())
        assert copies["A"].regions[0].rubric == ta.regions[0].rubric and copies["A"].pages == ta.pages
        assert db.get_hand_setup(copies["A"].id, uid)[0] == "choice"        # multiple choice boxes too
        assert db.get_exam_strictness("Midterm", b) == 7
        assert len(db.list_versions("Midterm", home)) == 2                   # the original stays
        with pytest.raises(ValueError):
            db.copy_exam("Midterm", home, b)                                # name taken there
        assert db.copy_exam("Midterm", home, b, "Midterm (section 2)")

        quiz, p = _exam(data_dir, "Quiz to move", home)
        sub = Submission(exam_name="Quiz to move", template_id=quiz.id, status=SUB_NEW, scan_pages=[p])
        db.add_submission(sub)
        db.move_exam("Quiz to move", home, b)
        assert "Quiz to move" not in db.list_exams(home) and "Quiz to move" in db.list_exams(b)
        assert db.get_submission(sub.id).course_id == b and db.load_template(quiz.id).course_id == b
        with pytest.raises(ValueError):
            db.move_exam("Midterm", home, b)                                # B has a Midterm now
    finally:
        db.set_hand_setup(ta.id, uid, kind="", choices=[])
        db.set_exam_strictness("Midterm", 10, home)


def test_course_names_and_deleting_only_empty_courses(course_b):
    home, b = course_b
    with pytest.raises(ValueError):
        db.add_course(db.course_name(home).upper())                        # names are unique
    db.rename_course(b, db.course_name(b) + " (renamed)")
    assert db.course_name(b).endswith("(renamed)")
    with pytest.raises(ValueError):
        db.delete_course(home)                                              # it has students and exams
    sid = db.add_student(Student(name="Mover", course_id=b))
    with pytest.raises(ValueError):
        db.delete_course(b)
    db.move_students([sid], home)
    assert "Mover" in {s.name for s in db.list_students(home)} and not db.list_students(b)
    db.delete_student(sid)
    c = db.add_course(f"Temporary {time.time_ns()}")
    db.delete_course(c)
    assert c not in [x["id"] for x in db.list_courses()]


def test_manage_courses_dialog(qapp, course_b):
    from lmgrader.ui.courses_dialog import CoursesDialog
    dlg = CoursesDialog(None, db.current_course())
    n = dlg.list.count()
    cid = dlg.new_course(f"Dialog course {time.time_ns()}")
    assert dlg.created == cid and dlg.list.count() == n + 1
    dlg.rename(cid, "Dialog course renamed")
    assert any("Dialog course renamed" in dlg.list.item(r).text() for r in range(dlg.list.count()))
    dlg.delete(cid)
    assert dlg.list.count() == n
    dlg.close()


def test_courses_dialog_can_delete_the_course_selected_at_start(qapp, course_b):
    # the course highlighted when the dialog opens is first in the list: Delete must not wait for a
    # click elsewhere (it was enabled only when the list had 2+ rows at the moment it was selected)
    from lmgrader.ui.courses_dialog import CoursesDialog
    first = db.list_courses()[0]["id"]
    dlg = CoursesDialog(None, first)
    try:
        assert dlg.list.count() > 1 and dlg.list.currentRow() == 0 and dlg._current_id() == first
        assert dlg.btn_delete.isEnabled() and dlg.btn_rename.isEnabled()
    finally:
        dlg.close()


def _test_of(exam, course_id, template_id=None, graded=False):
    sub = Submission(exam_name=exam, status=SUB_NEW, course_id=course_id, template_id=template_id)
    db.add_submission(sub)
    if graded:
        from lmgrader.models import Grade
        db.save_grade(Grade(submission_id=sub.id, region_id=0, problem="Q1", score=1, max_points=2))
    return sub.id


def test_deleting_the_last_version_deletes_the_exams_tests(qapp, course_b, data_dir, monkeypatch):
    """Deleting an exam used to leave its scans in the DB: no tab showed them and the course could
    never be deleted. Now the exam's last version takes them (after a warning)."""
    from PyQt6.QtWidgets import QMessageBox
    from lmgrader.ui import template_editor
    from lmgrader.ui.template_editor import TemplateEditorView
    home, b = course_b
    home_midterm = db.exam_test_count("Midterm", home)
    va, p = _exam(data_dir, "Midterm", b)                    # same name as the home course's exam
    vb = create_template_from_image("Midterm vB", p, "Midterm", "B")
    vb.course_id = b
    vb.id = persist(vb)
    graded = _test_of("Midterm", b, va.id, graded=True)
    unread = _test_of("Midterm", b)
    asked = []
    monkeypatch.setattr(template_editor.QMessageBox, "question",
                        staticmethod(lambda *a, **k: (asked.append(("question", a[2])), QMessageBox.StandardButton.Yes)[1]))
    answer = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(template_editor.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: (asked.append(("warning", a[2])), answer[0])[1]))
    db.set_current_course(b)
    v = TemplateEditorView()
    try:
        v.load(va.id)
        v.delete_version()                                    # vB is left: the tests stay
        assert asked[-1][0] == "question" and db.load_template(va.id) is None
        assert db.exam_test_count("Midterm", b) == 2 and db.list_grades(graded)
        v.load(vb.id)
        v.delete_version()                                    # the last version: warned, Cancel
        assert asked[-1][0] == "warning" and "2 scanned tests" in asked[-1][1] and "can't be undone" in asked[-1][1]
        assert db.load_template(vb.id) is not None and db.exam_test_count("Midterm", b) == 2
        answer[0] = QMessageBox.StandardButton.Yes
        v.delete_version()
        assert db.load_template(vb.id) is None and db.exam_test_count("Midterm", b) == 0
        assert db.get_submission(graded) is None and db.get_submission(unread) is None and not db.list_grades(graded)
        assert db.exam_test_count("Midterm", home) == home_midterm          # the other course's exam is untouched
        db.delete_course(b)                                    # nothing is left in the course
        assert b not in [c["id"] for c in db.list_courses()]
    finally:
        v.dirty = False
        v.close()


def test_course_with_tests_of_deleted_exams_can_be_deleted(qapp, course_b, monkeypatch):
    """Scans left by exams deleted before this fix: the course is deleted with them, after a warning."""
    from PyQt6.QtWidgets import QMessageBox
    from lmgrader.ui import courses_dialog
    from lmgrader.ui.courses_dialog import CoursesDialog
    home, b = course_b
    left = [_test_of("Gone exam", b, template_id=987654, graded=True), _test_of(None, b)]
    told = []
    monkeypatch.setattr(courses_dialog.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: (told.append(a[2]), QMessageBox.StandardButton.Yes)[1]))
    dlg = CoursesDialog(None, b)
    try:
        row = next(dlg.list.item(r).text() for r in range(dlg.list.count()) if dlg.list.item(r).data(0x0100) == b)
        assert "2 scanned tests (of deleted exams)" in row
        assert dlg._current_id() == b and dlg.btn_delete.isEnabled()
        dlg._ask_delete()
        assert told and "2 scanned tests of deleted exams" in told[0] and "can't be undone" in told[0]
        assert b not in [c["id"] for c in db.list_courses()]
        assert all(db.get_submission(i) is None for i in left) and not db.list_grades(left[0])
        with pytest.raises(ValueError, match="students"):
            db.delete_course(home)                             # students and exams still block
    finally:
        dlg.close()


def test_sidebar_switches_course_in_every_tab(qapp, course_b, data_dir, monkeypatch):
    from lmgrader.ui import main_window
    from lmgrader.ui.main_window import MainWindow
    home, b = course_b
    monkeypatch.setitem(UI_CONFIG, "course_id", None)
    bio, _ = _exam(data_dir, "Bio quiz", b)
    zed = db.add_student(Student(name="Zed Course-B", course_id=b))
    told = []
    monkeypatch.setattr(main_window.QMessageBox, "information", staticmethod(lambda *a: told.append(a[2])))
    mw = MainWindow()
    rows = {mw.nav.item(i).text(): i for i in range(mw.nav.count())}

    def go(tab):
        mw.nav.setCurrentRow(rows[tab])
        qapp.processEvents()

    try:
        assert mw.cb_course.currentData() == home
        go("Students")
        assert mw.switch_course(b)
        assert db.current_course() == b and UI_CONFIG["course_id"] == b and mw.cb_course.currentData() == b
        sv = mw.students_view
        assert [sv.table.item(r, 0).text() for r in range(sv.table.rowCount())] == ["Zed Course-B"]
        tv = mw.template_view
        assert [tv.tree.topLevelItem(i).text(0) for i in range(tv.tree.topLevelItemCount())] == ["Bio quiz"]
        assert tv.t is not None and tv.t.id == bio.id
        go("Hand Grade")
        assert [mw.hand_view.cb_exam.itemText(i) for i in range(mw.hand_view.cb_exam.count())] == ["Bio quiz"]
        go("Review")
        assert [mw.review_view.cb_exam.itemData(i) for i in range(mw.review_view.cb_exam.count())] == [None, "Bio quiz"]
        go("Scan & Names")
        assert mw.scan_view.exam == "Bio quiz" and [s.name for s in mw.scan_view.students] == ["Zed Course-B"]

        # a running job keeps the course
        mw.scan_view.worker = object()
        assert not mw.switch_course(home) and db.current_course() == b and mw.cb_course.currentData() == b
        assert told and "Scan & Names" in told[-1]
        mw.scan_view.worker = None

        # copy the exam to the home course, move the student there
        assert tv.copy_or_move_exam("Bio quiz", home, "copy")
        sv.move_to([zed], home)
        assert sv.table.rowCount() == 0
        assert mw.switch_course(home)
        go("Hand Grade")
        exams = [mw.hand_view.cb_exam.itemText(i) for i in range(mw.hand_view.cb_exam.count())]
        assert "Bio quiz" in exams and "Midterm" in exams
        assert "Zed Course-B" in {s.name for s in db.list_students()}
    finally:
        mw.template_view.dirty = False
        mw.close()
        db.set_current_course(home)
        for t in db.list_versions("Bio quiz", home):
            db.delete_template(t.id)
        db.delete_student(zed)
