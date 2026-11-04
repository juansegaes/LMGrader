"""Version field: the version a test states wins over layout matching; a human can set it by hand."""
import cv2
from lmgrader import db
from lmgrader.grading import session
from lmgrader.llm.client import match_version_label
from lmgrader.models import Grade, Student, SUB_IDENTIFIED, SUB_READY, SUB_GRADED
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, duplicate_as_version, persist
from conftest import FakeLLM, make_page

EXAM = "Version Field Exam"
VFIELD = (520, 1040, 300, 50)

def _page(label):
    img = make_page(77, "SAME LAYOUT")          # identical layout: ORB can't tell A from B
    cv2.putText(img, f"VERSION {label}", (530, 1075), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    return img

class VersionLLM(FakeLLM):
    def __init__(self, version, name="Vera Fieldman"):
        super().__init__(name)
        self.version, self.version_calls = version, 0

    def read_version(self, b64, labels):
        self.version_calls += 1
        assert labels == ["A", "B"]
        return self.version

def _setup(tmp_path):
    exam = f"{EXAM} {tmp_path.name}"     # the DB is shared by the whole run: one exam per test
    pa, pb = str(tmp_path / "va.png"), str(tmp_path / "vb.png")
    save_image(pa, _page("A")); save_image(pb, _page("B"))
    a = create_template_from_image("vf A", [pa], exam, "A")
    a.version_rect, a.name_rect = VFIELD, (500, 100, 300, 60)
    a.id = persist(a)
    b = duplicate_as_version(a, [pb], "B")
    assert b.version_rect == VFIELD                       # copied to the new version
    b.id = persist(b)
    if not any(s.name == "Vera Fieldman" for s in db.list_students()):
        db.add_student(Student(name="Vera Fieldman"))
    scan = str(tmp_path / "scan.png"); save_image(scan, _page("B"))
    sub = session.create_submissions(exam, [scan], 1)[0][0]
    return db.list_versions(exam), sub

def test_version_rect_persisted(tmp_path):
    versions, _ = _setup(tmp_path)
    assert [v.version_rect for v in versions] == [VFIELD, VFIELD]

def test_field_decides_the_version(tmp_path):
    versions, sub = _setup(tmp_path)
    vb = next(v for v in versions if v.version_label == "B")
    llm = VersionLLM("B")
    res = session.identify_submission(sub, versions, db.list_students(), llm)
    assert llm.version_calls == 1
    assert res.template_id == vb.id and res.version_read == "field: B" and res.status == SUB_READY

def test_unreadable_field_needs_a_human(tmp_path):
    versions, sub = _setup(tmp_path)
    vb = next(v for v in versions if v.version_label == "B")
    res = session.identify_submission(sub, versions, db.list_students(), VersionLLM(""))
    assert res.status == SUB_IDENTIFIED and session.version_hold(res) and "unreadable" in res.version_read
    session.confirm_student(res, res.student_id)          # confirming the name doesn't skip the check
    assert db.get_submission(res.id).status == SUB_IDENTIFIED
    session.set_version(db.get_submission(res.id), vb)
    got = db.get_submission(res.id)
    assert got.status == SUB_READY and got.version_confirmed and not session.version_hold(got)

def test_manual_version_sticks_and_drops_old_grades(tmp_path):
    versions, sub = _setup(tmp_path)
    va, vb = sorted(versions, key=lambda v: v.version_label)
    res = session.identify_submission(sub, versions, db.list_students(), VersionLLM("B"))
    db.save_grade(Grade(submission_id=res.id, region_id=0, problem="Q1", score=2, max_points=4))
    res.status = SUB_GRADED; db.update_submission(res)
    session.set_version(res, va)                          # human: it's really version A
    got = db.get_submission(res.id)
    assert got.template_id == va.id and got.version_read == "manual" and got.status == SUB_READY
    assert db.list_grades(res.id) == []                   # graded against B: no longer valid
    llm = VersionLLM("B")
    again = session.identify_submission(got, versions, db.list_students(), llm)   # "Re-read"
    assert again.template_id == va.id and llm.version_calls == 0

def test_match_version_label():
    assert match_version_label("Version b", ["A", "B"]) == "B"
    assert match_version_label("FORM 2", ["1", "2"]) == "2"
    assert match_version_label("unknown", ["A", "B"]) == ""
    assert match_version_label("", ["A", "B"]) == ""
