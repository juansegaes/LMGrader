"""Persisted grading session: splitting, identification, confirmation, crash recovery, resume."""
import os
from lmgrader import db
from lmgrader.grading import session, Grader
from lmgrader.models import SUB_NEW, SUB_IDENTIFIED, SUB_READY, SUB_GRADING, SUB_GRADED
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page


def test_split_pages():
    pages = [f"p{i}" for i in range(7)]
    tests, left = session.split_pages(pages, 3)
    assert tests == [(["p0", "p1", "p2"], []), (["p3", "p4", "p5"], [])] and left == ["p6"]
    tests, left = session.split_pages(pages[:4], 2, duplex=True)
    assert tests == [(["p0", "p2"], ["p1", "p3"])] and left == []


def test_split_by_layout():
    from lmgrader.models import BLANK, double_sided_layout
    pages = [f"s{i}" for i in range(17)]
    # the real exam: 4 template pages scanned double-sided -> 8 pages per test
    tests, left = session.split_by_layout(pages, double_sided_layout(4))
    assert len(tests) == 2 and left == ["s16"]
    assert tests[0] == (["s0", "s2", "s4", "s6"], ["s1", "s3", "s5", "s7"])
    # a single blank scratch page after page 2 only; a leading blank goes to the extras
    tests, _ = session.split_by_layout(["a", "b", "x", "c"], [0, 1, BLANK, 2])
    assert tests == [(["a", "b", "c"], ["", "x", ""])]
    tests, _ = session.split_by_layout(["x", "a", "y", "z"], [BLANK, 0, BLANK, BLANK])
    assert tests == [(["a"], ["y", "x", "z"])]
    # create_submissions accepts a template (uses its scan layout)
    from lmgrader.models import TestTemplate
    t = TestTemplate(image_path="p0.png", page_paths=["p0.png", "p1.png"], scan_layout=[0, BLANK, 1, BLANK])
    assert t.scan_page_count == 4 and session.layout_of(t) == [0, BLANK, 1, BLANK]


class IdLLM(FakeLLM):
    def __init__(self, sid, name):
        super().__init__(name)
        self.sid, self.name_reads = sid, 0

    def read_student_id(self, b):
        return self.sid

    def read_name(self, b):
        self.name_reads += 1
        return super().read_name(b)


def test_match_student_id_first():
    from lmgrader.models import Student
    from lmgrader.students.matching import match_student
    roster = [Student(id=1, name="David Krige", student_id="801560603"),
              Student(id=2, name="Daniel Kruger", student_id="801560630"),
              Student(id=3, name="Addy Tran", student_id="801455376"),
              Student(id=4, name="Sejal Grover", student_id="801400202")]
    # exact ID -> accepted even if the name is badly read
    m = match_student(IdLLM("801 560 603", "Dvd Krge"), "id", "name", roster)
    assert m.student.id == 1 and m.method == "id" and not m.needs_confirm and m.id_read == "801560603"
    # exact ID but the name clearly belongs to someone else -> confirm
    m = match_student(IdLLM("801560603", "Addy Tran"), "id", "name", roster)
    assert m.student.id == 1 and m.needs_confirm
    # one digit off, unique, name agrees -> accepted
    m = match_student(IdLLM("801455375", "Addy Tran"), "id", "name", roster)
    assert m.student.id == 3 and m.method == "id~1" and not m.needs_confirm
    # one digit off but the name does not agree -> falls back to name matching (+ confirm)
    m = match_student(IdLLM("801455375", "Sejal Grover"), "id", "name", roster)
    assert m.student.id == 4 and m.needs_confirm
    # blank ID field -> name decides on its own
    m = match_student(IdLLM("", "Sejal Grover"), "id", "name", roster)
    assert m.student.id == 4 and m.method == "fuzzy" and not m.needs_confirm and m.id_read == ""
    # no ID field on the template
    m = match_student(IdLLM("", "Addy Tran"), None, "name", roster)
    assert m.student.id == 3 and not m.needs_confirm


def test_two_page_exam_session(tmp_path):
    # 2-page template: name on page 1, Q1 on page 1, Q2 on page 2
    p1, p2 = str(tmp_path / "t1.png"), str(tmp_path / "t2.png")
    save_image(p1, make_page(11, "PAGE ONE")); save_image(p2, make_page(12, "PAGE TWO"))
    t = create_template_from_image("Final vA", [p1, p2], "Final", "A")
    assert t.page_count == 2
    for page, label in ((0, "Q1"), (1, "Q2")):
        r = add_region(t, label, "g", (100, 300, 300, 150), 4, rubric="- [4 pt] both roots")
        r.page, r.question_text = page, r"Solve $x^2-4=0$."
    t.name_rect = (500, 100, 300, 60)
    t.id = persist(t)
    assert db.load_template(t.id).regions[1].page == 1 and db.load_template(t.id).page_count == 2

    # one upload with two students' tests back to back (4 pages)
    scans = []
    for i, (seed, txt) in enumerate([(11, "PAGE ONE"), (12, "PAGE TWO")] * 2):
        sp = str(tmp_path / f"s{i}.png"); save_image(sp, make_page(seed, txt)); scans.append(sp)
    subs, left = session.create_submissions("Final", scans, 2, source="batch.pdf")
    assert len(subs) == 2 and not left
    assert all(s.status == SUB_NEW for s in db.list_submissions_by_status("Final"))

    # unique name: the roster is shared across the test session
    from lmgrader.models import Student
    db.add_student(Student(name="Zelda Quartermaine"))
    students = db.list_students()
    versions = db.list_versions("Final")
    a = session.identify_submission(subs[0], versions, students, FakeLLM("Zelda Quartermaine"))
    b = session.identify_submission(subs[1], versions, students, FakeLLM("zzzz qqq"))
    assert a.status == SUB_READY and a.name_confirmed
    assert b.status == SUB_IDENTIFIED and not b.name_confirmed   # human must complete it
    session.confirm_student(b, students[0].id)
    assert db.get_submission(b.id).status == SUB_READY

    # crash midway: stop after the first region, mark as grading, then recover
    calls = []
    llm = FakeLLM("")
    orig = llm.grade_answer
    def counting(*args, **kw):
        calls.append(args[1]); return orig(*args, **kw)
    llm.grade_answer = counting
    graded_once = []
    session.grade_one(Grader(llm), a, progress_cb=lambda i, n: graded_once.append(i),
                      should_stop=lambda: len(graded_once) >= 1)
    assert db.get_submission(a.id).status == SUB_READY and calls == ["Q1"]
    sub = db.get_submission(a.id); sub.status = SUB_GRADING; db.update_submission(sub)   # simulated crash
    assert session.recover_interrupted() == 1
    sub = db.get_submission(a.id)
    assert sub.status == SUB_READY
    session.grade_one(Grader(llm), sub)
    assert calls == ["Q1", "Q2"]           # Q1 was not graded twice
    sub = db.get_submission(a.id)
    assert sub.status == SUB_GRADED and len(sub.aligned_pages) == 2
    assert [g.problem for g in db.list_grades(a.id)] == ["Q1", "Q2"]
