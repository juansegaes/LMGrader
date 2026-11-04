"""End-to-end smoke tests with a fake LLM (no LM Studio needed)."""
import time

import cv2
import pytest

from conftest import FakeLLM, SCREENSHOT_DIR

def test_legacy_db_migrated():
    from lmgrader import db
    old = next(t for t in db.list_templates() if t.name == "Old quiz")
    assert old.exam_name == "Old quiz"
    assert old.version_label == "A"
    assert old.regions[0].question_text == ""
    assert old.name_rect is None

def test_versions(world):
    from lmgrader import db
    assert [v.version_label for v in db.list_versions("Midterm")] == ["A", "B"]
    tb = db.load_template(world["tb_id"])
    assert tb.regions[0].question_rect == (100, 200, 400, 80)
    assert tb.name_rect == (500, 100, 300, 60)

def test_identify_version_and_clear_name(world):
    from lmgrader import db
    from lmgrader.grading import identify
    ident = identify(world["scan"], db.list_versions("Midterm"), db.list_students(), FakeLLM("Bob Smith"))
    assert ident.template.version_label == "B"
    assert ident.alignment_ok
    assert ident.scores["B"] > ident.scores.get("A", 0)
    assert ident.name.student.name == "Bob Smith"
    assert ident.name.method == "fuzzy" and not ident.name.needs_confirm

def test_identify_ambiguous_name_uses_llm_pick(world):
    from lmgrader import db
    from lmgrader.grading import identify
    llm = FakeLLM("B. Smth")
    ident = identify(world["scan"], db.list_versions("Midterm"), db.list_students(), llm)
    assert ident.name.student.name == "Bob Smith"
    assert ident.name.method == "llm"
    assert llm.picks == 1

def test_fuzzy_handles_accents(world):
    from lmgrader import db
    from lmgrader.students.matching import fuzzy_candidates
    student, score = fuzzy_candidates("carlos diaz", db.list_students())[0]
    assert student.name == "Carlos Díaz" and score > 85

def test_two_stage_question_then_rubric(world, monkeypatch):
    """Rubric job extracts the missing question first (stage 1), then writes the rubric (stage 2)."""
    from lmgrader import db
    from lmgrader.template.template import persist
    from lmgrader.ui import template_editor as te

    class Sig:
        def __init__(self):
            self.got = []
        def emit(self, *a):
            self.got.append(a)

    class W:
        def __init__(self):
            self.progress, self.item = Sig(), Sig()

    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: FakeLLM()))
    tb = db.load_template(world["tb_id"])
    w = W()
    te.job_rubric(cv2.imread(world["pb"]), [(0, tb.regions[0])], worker=w)
    payloads = [a[0] for a in w.item.got]
    assert [p[0] for p in payloads] == ["question", "rubric"]
    tb.regions[0].question_text = payloads[0][2]
    tb.regions[0].rubric = payloads[1][2]
    persist(tb)
    assert "x^2" in db.load_template(world["tb_id"]).regions[0].question_text

def test_grading_uses_fixed_question_and_rubric(world):
    from lmgrader import db
    from lmgrader.grading import Grader
    from lmgrader.models import Submission
    tb = db.load_template(world["tb_id"])
    if not tb.regions[0].rubric:  # allow running this test on its own
        tb.regions[0].question_text = r"Solve $x^2-4=0$."
        tb.regions[0].rubric = "- [4 pt] both roots"
        db.save_template(tb)
    bob = next(s for s in db.list_students() if s.name == "Bob Smith")
    sub = Submission(student_id=bob.id, template_id=tb.id, scan_front_path=world["scan"])
    sub.id = db.add_submission(sub)
    Grader(FakeLLM()).grade_submission(db.load_template(tb.id), sub)  # FakeLLM asserts question/rubric present
    db.update_submission(sub)
    g = db.list_grades(sub.id)[0]
    assert g.score == 3 and g.transcription == "x=2,-2" and not g.needs_human_review

def test_client_parsing_helpers():
    from lmgrader.llm.client import _extract_json, format_rubric, LLMClient
    assert _extract_json('<think>{"a":1}</think>{"score": 2}') == {"score": 2}
    parsed = LLMClient()._parse('```json\n{"score": 9, "feedback":"x","needs_human_review":false,'
                                '"review_reason":"","transcription":"t"}\n```', 4)
    assert parsed["score"] == 4  # clamped to max points
    text = format_rubric({"expected_answer": "2", "criteria": [{"description": "a", "points": 1},
                                                               {"description": "b", "points": 2}]}, 3)
    assert "- [1 pt] a" in text and "Note" not in text
    assert "Note" in format_rubric({"expected_answer": "", "criteria": [{"description": "a", "points": 1}]}, 3)

def test_ui_smoke(world, qapp):
    from lmgrader import db
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow

    # make sure there's a graded submission to review
    if not db.list_submissions(template_id=world["tb_id"]):
        test_grading_uses_fixed_question_and_rubric(world)

    mw = MainWindow()
    mw.resize(1400, 880)
    mw.show()
    for i in range(5):
        mw.nav.setCurrentRow(i)
        qapp.processEvents()

    tv = mw.template_view
    tv.load(world["tb_id"])
    qapp.processEvents()
    n_regions = tv.region_list.count()
    assert n_regions >= 1
    tv.region_list.setCurrentRow(0)
    tv.in_rubric.setPlainText("new rubric")
    assert tv.dirty and tv.t.regions[0].rubric == "new rubric"
    tv.mode_group.button(te.MODE_ANSWER).setChecked(True)
    tv.on_rect_drawn((100, 600, 200, 100))
    assert len(tv.t.regions) == n_regions + 1 and tv.sel == n_regions
    tv.save(quiet=True)
    assert not tv.dirty

    sv = mw.scan_view
    mw.nav.setCurrentRow(2)
    qapp.processEvents()
    sv.cb_exam.setCurrentIndex(sv.cb_exam.findText("Midterm"))
    qapp.processEvents()
    sv.chk_names.setChecked(False)  # no LLM in this test
    new_ids = sv.add_pages([world["scan"], world["pa"]])   # 1-page exam -> two tests
    t0 = time.time()
    while sv.worker is not None and time.time() - t0 < 30:
        qapp.processEvents()
        time.sleep(0.05)
    by_id = {s.id: s for s in db.list_submissions_by_status("Midterm")}
    labels = [db.load_template(by_id[i].template_id).version_label for i in new_ids]
    assert labels == ["B", "A"]
    sv.table_upload.selectRow(0)
    qapp.processEvents()
    for sid in new_ids:   # keep later tests' Midterm data clean
        db.delete_submission(sid)
    sv.reload()

    rv = mw.review_view
    mw.nav.setCurrentRow(3)
    qapp.processEvents()
    rv.cb_exam.setCurrentIndex(rv.cb_exam.findData("Midterm"))
    qapp.processEvents()
    assert rv.table.rowCount() >= 1
    rv.table.selectRow(0)
    qapp.processEvents()
    assert rv.detail_layout.count() >= 2

    SCREENSHOT_DIR.mkdir(exist_ok=True)
    for i, name in enumerate(["templates", "students", "scan", "review", "settings"]):
        mw.nav.setCurrentRow(i)
        qapp.processEvents()
        assert mw.grab().save(str(SCREENSHOT_DIR / f"shot_{name}.png"))
    mw.template_view.dirty = False
    mw.close()
