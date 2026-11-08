"""Grading strictness per exam (5 very generous .. 10 strict) -> the grader's system prompt."""
from lmgrader import db
from lmgrader.config import LLM_CONFIG, clamp_strictness
from lmgrader.grading import session, Grader
from lmgrader.llm.client import LLMClient, STRICTNESS_TEXT, grader_system
from lmgrader.template.alignment import save_image
from conftest import FakeLLM, make_page
from test_instructions import editor, page, _setup_region, wait_for   # noqa: F401  (fixtures)

ORIGINAL = ("Grade strictly according to the rubric you are given. Award partial credit only where the "
            "rubric allows it.")

def test_prompts_per_level():
    assert ORIGINAL in grader_system(10)                  # level 10 keeps today's wording
    for lvl in range(5, 11):
        s = grader_system(lvl)
        assert STRICTNESS_TEXT[lvl] in s
        assert "crossed out" in s and "follow the professor's instructions" in s
        assert all(STRICTNESS_TEXT[o] not in s for o in range(5, 11) if o != lvl)
    assert "Be very generous" in grader_system(5)
    assert clamp_strictness(0) == 5 and clamp_strictness(12) == 10 and clamp_strictness(None) == 10

def test_stored_per_exam():
    assert db.get_exam_strictness("Never configured exam") == 10
    db.set_exam_instructions("Strict store", "Some instructions.")
    db.set_exam_strictness("Strict store", 6)
    assert db.get_exam_strictness("Strict store") == 6
    assert db.get_exam_instructions("Strict store") == "Some instructions."   # untouched
    db.set_exam_strictness("Strict store", 2)
    assert db.get_exam_strictness("Strict store") == 5

def test_grade_answer_sends_the_level():
    c = LLMClient(base_url="http://127.0.0.1:9/v1", api_key="x")
    systems = []
    c._chat = lambda messages, *a, **k: (systems.append(messages[0]["content"]),
                                         '{"score": 1, "feedback": "ok", "transcription": "x"}')[1]
    c.grade_answer("aGk=", "1", "g", "- [2 pt] x", 2)
    c.grade_answer("aGk=", "1", "g", "- [2 pt] x", 2, strictness=5)
    assert ORIGINAL in systems[0] and STRICTNESS_TEXT[5] in systems[1]

def test_scoring_step_follows_the_level():
    """The level also shapes step 2 of the request: below 10 criteria are not all-or-nothing, and
    at 5/6 a sensible attempt has a floor (the system paragraph alone left zeros at level 5)."""
    from lmgrader.llm.back_pages import grade_with_continuation
    c = LLMClient(base_url="http://127.0.0.1:9/v1", api_key="x")
    users = []
    c._chat = lambda messages, *a, **k: (users.append(messages[1]["content"][0]["text"]),
                                         '{"score": 1, "feedback": "ok", "transcription": "x"}')[1]
    for lvl in (10, 7, 6, 5):
        c.grade_answer("aGk=", "1", "g", "- [2 pt] x", 2, strictness=lvl)
    grade_with_continuation(c, "aGk=", "aGk=", "1", "g", "- [2 pt] x", 2, strictness=5)
    s10, s7, s6, s5, back5 = users
    assert "Apply each rubric criterion" in s10 and "all-or-nothing" not in s10    # unchanged at 10
    for s in (s7, s6, s5, back5):
        assert "not as all-or-nothing checks" in s and "Apply each rubric criterion" not in s
    assert "at least half of the maximum points" in s5 and "at least half" in back5
    assert "at least a quarter" in s6 and "override these minimums" in s6
    assert "at least" not in s7.split("2. Grading standard")[1].split("3.")[0]

class LevelLLM(FakeLLM):
    def __init__(self):
        super().__init__("")
        self.levels = []

    def grade_answer(self, *a, strictness=10, **kw):
        self.levels.append(strictness)
        return super().grade_answer(*a, **kw)

def test_editor_setting_reaches_the_grader(editor, page, tmp_path):
    tv, _, qapp, _ = editor
    t = tv.create_exam("Strictness exam", [page])
    assert tv.cb_strictness.currentData() == 10 and tv.cb_strictness.isEnabled()
    tv.cb_strictness.setCurrentIndex(tv.cb_strictness.findData(6))
    assert db.get_exam_strictness("Strictness exam") == 6
    _setup_region(tv)
    tv.rubric_editor.add_item(4, "both roots")
    tv.flush()
    scan = str(tmp_path / "scan.png"); save_image(scan, make_page(300, "INSTRUCTIONS"))
    sub = session.create_submissions("Strictness exam", [scan], 1)[0][0]
    sub.template_id = t.id
    llm = LevelLLM()
    Grader(llm).grade_submission(db.load_template(t.id), sub)
    assert llm.levels == [6]
    # switching exams shows each exam's own level
    tv.create_exam("Other strictness", [page])
    assert tv.cb_strictness.currentData() == 10

def test_default_strictness_for_new_exams(editor, page, monkeypatch):
    tv, _, qapp, _ = editor
    monkeypatch.setitem(LLM_CONFIG, "default_strictness", 7)
    tv.create_exam("Default strictness exam", [page])
    assert db.get_exam_strictness("Default strictness exam") == 7 and tv.cb_strictness.currentData() == 7
