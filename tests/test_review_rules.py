"""Flagged answers go to a human: continuation detection, confidence threshold, review UI, settings."""
import json
import pytest
from lmgrader import db, config
from lmgrader.config import LLM_CONFIG
from lmgrader.grading import session, Grader
from lmgrader.grading.grader import mentions_continuation, apply_review_rules
from lmgrader.models import Grade, Student
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

# ---------- continuation phrases ----------
POSITIVE = [
    "see back", "See back for the rest", "continued on the back", "cont. on next page",
    "P.T.O.", "turn over", "rest on the back", "ver atrás", "sigue al reverso",
    "continúa en la siguiente página", "ver detrás",
]
# Decisions: "back substitution" is ordinary math vocabulary, not a continuation; "continued
# fraction" and the Spanish abbreviation "pto." (punto) must not trigger either.
NEGATIVE = [
    "x = 2, solved by back substitution", "the continued fraction [1; 2, 2, 2]", "pto. de corte (2, 0)",
    "x^2 - 4 = 0 \\Rightarrow x = \\pm 2", "the integral converges", "f'(x) = 2x",
]

@pytest.mark.parametrize("text", POSITIVE)
def test_continuation_detected(text):
    assert mentions_continuation(text), text

@pytest.mark.parametrize("text", NEGATIVE)
def test_continuation_not_detected(text):
    assert not mentions_continuation(text), text

# ---------- rules ----------
def _g(conf=1.0, transcription="x = 2"):
    return Grade(score=3, max_points=4, confidence=conf, transcription=transcription)

def test_low_confidence_flags():
    g = _g(0.55)
    apply_review_rules(g, {}, 0.7)
    assert g.needs_human_review and "low_confidence (0.55)" in g.review_reason

def test_threshold_zero_disables():
    g = _g(0.1)
    apply_review_rules(g, {}, 0)
    assert not g.needs_human_review

def test_confident_answer_not_flagged():
    g = _g(0.95)
    apply_review_rules(g, {"continues_elsewhere": False}, 0.7)
    assert not g.needs_human_review and g.review_reason == ""

def test_model_continuation_flag():
    g = _g(0.95)
    apply_review_rules(g, {"continues_elsewhere": True}, 0.7)
    assert g.needs_human_review and "work_continues_elsewhere" in g.review_reason

def test_transcription_continuation_overrides_model():
    g = _g(0.99, "x = 2 (see back)")
    apply_review_rules(g, {"continues_elsewhere": False}, 0.7)   # model missed it; regex catches it
    assert g.needs_human_review and "work_continues_elsewhere" in g.review_reason

def test_full_marks_skip_review():
    """Full marks + option on: below the threshold is fine down to the floor; partial credit,
    below-floor and other reasons still go to a human."""
    full = lambda conf, tr="x = 2": Grade(score=4, max_points=4, confidence=conf, transcription=tr)
    g = full(0.5); apply_review_rules(g, {}, 0.7, full_marks_floor=0.3)
    assert not g.needs_human_review
    g = full(0.2); apply_review_rules(g, {}, 0.7, full_marks_floor=0.3)
    assert g.needs_human_review and "low_confidence (0.20)" in g.review_reason
    g = _g(0.5); apply_review_rules(g, {}, 0.7, full_marks_floor=0.3)          # 3/4: not full marks
    assert g.needs_human_review
    g = full(0.5); apply_review_rules(g, {}, 0.7)                              # option off
    assert g.needs_human_review
    g = full(0.5, "x = 2, see back"); apply_review_rules(g, {}, 0.7, full_marks_floor=0.3)
    assert g.needs_human_review and "low_confidence" not in g.review_reason

def test_review_settings(monkeypatch):
    from lmgrader.grading.grader import review_settings
    monkeypatch.setitem(LLM_CONFIG, "review_confidence", 0.8)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_skip_review", False)
    assert review_settings() == (0.8, None)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_skip_review", True)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_min_confidence", 0.3)
    assert review_settings() == (0.8, 0.3)

def test_reapply_full_marks_rules_on_saved_grades(monkeypatch):
    """Turning the full-marks options on also un-flags grades the AI already saved."""
    from lmgrader.grading.grader import reapply_full_marks_rules
    from lmgrader.models import Submission
    monkeypatch.setitem(LLM_CONFIG, "review_confidence", 0.7)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_skip_review", True)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_min_confidence", 0.3)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", True)
    sid = db.add_submission(Submission(exam_name="ReapplyExam", status="graded", has_work_on_back=True))
    def add(score, conf, reason, human=False):
        return db.save_grade(Grade(submission_id=sid, region_id=0, problem="1", score=score, max_points=4,
                                   confidence=conf, needs_human_review=True, review_reason=reason,
                                   human_graded=human))
    ids = {
        "low": add(4, 0.5, "low_confidence (0.50)"),                        # -> cleared
        "back": add(4, 0.9, "work_on_back"),                                # -> cleared
        "both": add(4, 0.5, "low_confidence (0.50); work_on_back (unlabeled)"),  # -> cleared
        "floor": add(4, 0.2, "low_confidence (0.20)"),                      # below the floor: kept
        "partial": add(3, 0.5, "low_confidence (0.50)"),                    # not full marks: kept
        "other": add(4, 0.5, "illegible; low_confidence (0.50)"),           # model's reason kept
        "human": add(4, 0.5, "low_confidence (0.50)", human=True),          # human's: untouched
    }
    assert reapply_full_marks_rules() >= 3      # the test DB is shared: other tests' grades may count
    by_id = {g.id: g for g in db.list_grades(sid)}
    for k in ("low", "back", "both"):
        assert not by_id[ids[k]].needs_human_review and by_id[ids[k]].review_reason == "", k
    for k in ("floor", "partial", "human"):
        assert by_id[ids[k]].needs_human_review, k
    assert by_id[ids["other"]].needs_human_review and by_id[ids["other"]].review_reason == "illegible"
    assert db.get_submission(sid).back_reviewed
    assert reapply_full_marks_rules() == 0      # idempotent

def test_reapply_full_marks_rules_options_off(monkeypatch):
    from lmgrader.grading.grader import reapply_full_marks_rules
    from lmgrader.models import Submission
    monkeypatch.setitem(LLM_CONFIG, "full_marks_skip_review", False)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", False)
    sid = db.add_submission(Submission(exam_name="ReapplyOff", status="graded"))
    db.save_grade(Grade(submission_id=sid, region_id=0, problem="1", score=4, max_points=4, confidence=0.5,
                        needs_human_review=True, review_reason="low_confidence (0.50); work_on_back"))
    assert reapply_full_marks_rules() == 0
    assert db.list_grades(sid)[0].needs_human_review

# ---------- end-to-end ----------
EXAM = "RulesExam"

class RulesLLM(FakeLLM):
    """Q1: confident and clean. Q2: low confidence. Q3: 'see back' in the transcription."""
    def grade_answer(self, b, problem, *a, **kw):
        res = super().grade_answer(b, problem, *a, **kw)
        res.update(confidence={"Q1": 0.95, "Q2": 0.4, "Q3": 0.9}[problem], continues_elsewhere=False)
        if problem == "Q3":
            res["transcription"] = "x = 2 ... see back"
        return res

@pytest.fixture(scope="module")
def rules_world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rules")
    p1 = str(tmp / "t1.png"); save_image(p1, make_page(31, "RULES PAGE"))
    t = create_template_from_image(f"{EXAM} vA", [p1], EXAM, "A")
    for i, lab in enumerate(("Q1", "Q2", "Q3")):
        r = add_region(t, lab, "g", (80, 200 + 230 * i, 300, 150), 4,
                       rubric=r"- [4 pt] both roots $x = \pm 2$")
        r.question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    sp = str(tmp / "s.png"); save_image(sp, make_page(31, "RULES PAGE"))
    bp = str(tmp / "back.png"); save_image(bp, make_page(32, "BACK"))
    subs, _ = session.create_submissions(EXAM, [sp, bp], 1, duplex=True, source="rules.pdf")
    sub = subs[0]
    sub.template_id = t.id
    session.confirm_student(sub, db.add_student(Student(name="Rules Student")))
    return {"sub": sub, "template": t}

def test_grader_routes_to_human(rules_world, monkeypatch):
    monkeypatch.setitem(LLM_CONFIG, "review_confidence", 0.7)
    sub = rules_world["sub"]
    session.grade_one(Grader(RulesLLM()), sub)
    g = {x.problem: x for x in db.list_grades(sub.id)}
    assert g["Q2"].needs_human_review and "low_confidence (0.40)" in g["Q2"].review_reason
    assert g["Q3"].needs_human_review and "work_continues_elsewhere" in g["Q3"].review_reason
    assert abs(g["Q2"].confidence - 0.4) < 1e-9
    # Q1 can only be flagged by the back-page ink check (the synthetic back page has ink)
    assert "low_confidence" not in g["Q1"].review_reason
    assert "work_continues_elsewhere" not in g["Q1"].review_reason

def test_review_card_accept_and_back_button(qapp, rules_world):
    from PyQt6.QtWidgets import QLabel
    from lmgrader.ui.review_view import ReviewView, describe_reasons
    sub = rules_world["sub"]
    v = ReviewView(); v.resize(1300, 900); v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findData(EXAM)); qapp.processEvents()
    row = next(i for i, (s, _) in enumerate(v._rows) if s.id == sub.id)
    assert v.table.item(row, 3).text().startswith("Needs human grade")
    assert v.table.item(row, 2).text().endswith("*")
    v.table.selectRow(row); qapp.processEvents()
    cards = {c.g.problem: c for c in v.grade_cards}
    q3 = cards["Q3"]
    texts = [l.text() for l in q3.findChildren(QLabel)]
    assert any("continues elsewhere" in t for t in texts)
    assert q3.btn_back is not None and q3.btn_back.isVisibleTo(q3)    # a back page was scanned
    assert "AI unsure (confidence 40%)" in describe_reasons(cards["Q2"].g.review_reason)
    for _ in range(3):
        qapp.processEvents()
    # the rubric is readable on flagged questions, and can be hidden
    q2 = cards["Q2"]
    assert q2.rubric_box.isVisibleTo(q2) and "both roots" in q2.lbl_rubric.text()
    q2.btn_rubric.click(); assert not q2.rubric_box.isVisibleTo(q2)
    q2.btn_rubric.click(); assert q2.rubric_box.isVisibleTo(q2)
    assert q2.btn_retry.isVisibleTo(q2) and q2.btn_retry.isEnabled()
    assert "<img" in q2.lbl_rubric.text()     # $x = \pm 2$ is drawn, not shown as code
    # "Next flagged" walks the flagged questions of this test, then moves on (here: the only
    # flagged test of the exam, so it wraps to its first flagged question)
    sb = v.detail_scroll.verticalScrollBar(); sb.setValue(0)
    pending = [c for c in v.grade_cards if c.g.needs_human_review]
    assert q2 in pending and q3 in pending
    for c in pending:
        assert v.next_flagged() == row and v._jump[0] is c
        assert sb.value() == min(c.y(), sb.maximum()) and sb.value() > 0
    assert v.next_flagged() == row and v._jump[0] is pending[0]
    for _ in pending[1:]:                    # leave the last one (Q3) on screen for the screenshot
        v.next_flagged()
    assert v._jump[0] is q3
    v.grab().save(str(SCREENSHOT_DIR / "review_human_grade.png"))
    q3.btn_ok.click(); qapp.processEvents()   # accept the AI suggestion
    g3 = next(x for x in db.list_grades(sub.id) if x.problem == "Q3")
    assert g3.human_graded and not g3.needs_human_review and g3.score == 3
    assert "Human" in q3.conf_pill.text()

class FullMarksLLM(FakeLLM):
    """Full marks for the problems in `full`, 1 point otherwise; always confident."""
    def __init__(self, full):
        super().__init__("")
        self.full = full

    def grade_answer(self, b, problem, group, rubric, pts, *a, **kw):
        res = super().grade_answer(b, problem, group, rubric, pts, *a, **kw)
        res.update(score=pts if problem in self.full else 1, confidence=0.95, continues_elsewhere=False,
                   needs_human_review=False, review_reason="")
        return res

def _back_world(tmp, name):
    """One-page exam (Q1, Q2) scanned double-sided with writing on the back."""
    p1 = str(tmp / f"{name}.png"); save_image(p1, make_page(41, "BACK RULE"))
    t = create_template_from_image(f"{name} vA", [p1], name, "A")
    for i, lab in enumerate(("Q1", "Q2")):
        add_region(t, lab, "g", (80, 200 + 300 * i, 300, 150), 4,
                   rubric="- [4 pt] both roots").question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    sp = str(tmp / f"{name}_s.png"); save_image(sp, make_page(41, "BACK RULE"))
    bp = str(tmp / f"{name}_b.png"); save_image(bp, make_page(42, "BACK WORK"))
    sub = session.create_submissions(name, [sp, bp], 1, duplex=True, source="b.pdf")[0][0]
    sub.template_id = t.id
    session.confirm_student(sub, db.add_student(Student(name=f"{name} Student")))
    return sub

@pytest.mark.parametrize("full, option, flagged, back_reviewed", [
    ({"Q1", "Q2"}, True, set(), True),              # all full marks: nothing for a human
    ({"Q1"}, True, {"Q2"}, False),                  # Q2 partial: still checked against the back
    ({"Q1", "Q2"}, False, {"Q1", "Q2"}, False),     # option off: old behaviour
])
def test_full_marks_ignore_back(tmp_path, monkeypatch, full, option, flagged, back_reviewed):
    from lmgrader.ui.review_view import _needs_review
    monkeypatch.setitem(LLM_CONFIG, "review_confidence", 0.7)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", option)
    sub = _back_world(tmp_path, f"BackRule{len(full)}{int(option)}")
    session.grade_one(Grader(FullMarksLLM(full)), sub)
    sub = db.get_submission(sub.id)
    grades = db.list_grades(sub.id)
    assert sub.has_work_on_back
    assert {g.problem for g in grades if "work_on_back" in g.review_reason} == flagged
    assert sub.back_reviewed == back_reviewed
    assert _needs_review(sub, grades) == bool(flagged)

def test_review_full_page_and_back(qapp, rules_world):
    """Every question can show its whole front page (answer box outlined) and the sheet's back,
    in one window that stays open while grading."""
    from lmgrader.ui.review_view import ReviewView
    sub = db.get_submission(rules_world["sub"].id)
    t = rules_world["template"]
    v = ReviewView(); v.resize(1300, 900); v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findData(EXAM)); qapp.processEvents()
    v.table.selectRow(next(i for i, (s, _) in enumerate(v._rows) if s.id == sub.id)); qapp.processEvents()
    cards = {c.g.problem: c for c in v.grade_cards}
    q1, q2 = cards["Q1"], cards["Q2"]
    assert q1.btn_front and q1.btn_back and q2.btn_front    # not only flagged questions
    q2.btn_front.click(); qapp.processEvents()
    dlg = v._page_dlg
    assert dlg.isVisible() and not dlg.isModal() and dlg.path == sub.aligned_pages[0]
    assert dlg.windowTitle() == "Q2 — page 1"
    assert [o.rect for o in dlg.viewer.canvas.overlays] == [tuple(t.regions[1].rect)]
    for _ in range(3):
        qapp.processEvents()
    dlg.grab().save(str(SCREENSHOT_DIR / "review_full_page.png"))
    q1.btn_back.click(); qapp.processEvents()
    assert v._page_dlg is dlg and dlg.path == sub.backs[0] and not dlg.viewer.canvas.overlays
    assert "back of page 1" in dlg.windowTitle()
    v.grab().save(str(SCREENSHOT_DIR / "review_page_buttons.png"))
    dlg.close(); v.close()

def test_no_back_scanned_message(qapp):
    from PyQt6.QtWidgets import QLabel
    from lmgrader.ui.review_view import GradeCard
    g = Grade(id=None, problem="Q9", score=2, max_points=4, needs_human_review=True,
              review_reason="work_continues_elsewhere", confidence=0.9)
    c = GradeCard(g, lambda: None, backs=[])
    assert c.btn_back is None
    assert any("No back page was scanned" in l.text() for l in c.findChildren(QLabel))

def test_pending_not_printed_in_feedback_pdf(rules_world):
    from lmgrader.export import feedback as fb
    g = Grade(problem="Q1", score=3, max_points=4, feedback="Looks good", needs_human_review=True)
    assert fb._score_text(g) == fb.PENDING and fb._shown_feedback(g) == ""
    g.human_graded, g.needs_human_review = True, False
    assert fb._score_text(g) == "3 / 4" and fb._shown_feedback(g) == "Looks good"
    total = fb._total_text([g, Grade(problem="Q2", score=4, max_points=4, needs_human_review=True)])
    assert total == "Total: 3 / 8 (1 pending review)"

def test_settings_persist_review_confidence(qapp, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QMainWindow
    from lmgrader.ui.settings_view import SettingsView
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setitem(LLM_CONFIG, "review_confidence", 0.7)
    monkeypatch.setitem(LLM_CONFIG, "default_instructions", "")
    import lmgrader.ui.settings_view as sv
    monkeypatch.setattr(sv, "settings_path", lambda: tmp_path / "settings.json")
    w = QMainWindow(); s = SettingsView(); w.setCentralWidget(s)
    assert s.in_conf.value() == 70
    s.in_conf.setValue(55)
    s.in_instructions.setPlainText("Drop points for work not shown.")
    s.save()
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert data["llm"]["review_confidence"] == 0.55
    assert data["llm"]["default_instructions"] == "Drop points for work not shown."

def test_settings_full_marks_option(qapp, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QMainWindow
    import lmgrader.ui.settings_view as sv
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(sv, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setitem(LLM_CONFIG, "full_marks_skip_review", False)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_min_confidence", 0.3)
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", True)
    w = QMainWindow(); s = sv.SettingsView(); w.setCentralWidget(s)
    assert not s.in_full_skip.isChecked() and s.in_full_floor.value() == 30 and not s.in_full_floor.isEnabled()
    s.in_full_skip.setChecked(True)
    assert s.in_full_floor.isEnabled()
    s.in_full_floor.setValue(25)
    assert s.in_full_back.isChecked()                  # on by default
    s.in_full_back.setChecked(False)
    s.save()
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["llm"]
    assert data["full_marks_skip_review"] is True and data["full_marks_min_confidence"] == 0.25
    assert data["full_marks_ignore_back"] is False
    s.in_full_back.setChecked(True)
    w.resize(1100, 900); w.show(); qapp.processEvents()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    w.grab().save(str(SCREENSHOT_DIR / "settings_full_marks.png"))
    w.close()
