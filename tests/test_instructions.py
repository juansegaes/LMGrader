"""Professor's grading instructions (per exam) and grader notes (per question)."""
import time
import pytest
from lmgrader import db
from lmgrader.config import LLM_CONFIG
from lmgrader.grading import session, Grader
from lmgrader.template.alignment import save_image
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

INSTR = "Make sure all work is shown; drop points for answers without supporting work."


class CaptureLLM(FakeLLM):
    """Records the kwargs passed to generate_rubric / grade_answer."""
    def __init__(self, read=""):
        super().__init__(read)
        self.rubric_calls, self.grade_calls = [], []

    def generate_rubric(self, q, p, prob, instructions="", notes=""):
        self.rubric_calls.append({"problem": prob, "instructions": instructions, "notes": notes})
        return super().generate_rubric(q, p, prob)

    def grade_answer(self, b, problem, group, rubric, pts, question="", instructions="", notes="", **kw):
        self.grade_calls.append({"problem": problem, "instructions": instructions, "notes": notes})
        return super().grade_answer(b, problem, group, rubric, pts, question)


def wait_for(qapp, cond, timeout=10.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert cond(), "timed out"


@pytest.fixture
def page(tmp_path):
    p = str(tmp_path / "blank.png")
    save_image(p, make_page(300, "INSTRUCTIONS"))
    return p


@pytest.fixture
def editor(qapp, monkeypatch):
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    llm = CaptureLLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    mw = MainWindow()
    mw.resize(1400, 880)
    mw.show()
    qapp.processEvents()
    yield mw.template_view, llm, qapp, mw
    mw.template_view.flush()
    mw.close()


def _setup_region(tv, notes="Accept degrees or radians."):
    tv.on_rect_drawn((100, 300, 300, 150))
    tv.region_list.setCurrentRow(0)
    tv.in_question.setPlainText(r"Solve $x^2-4=0$.")
    tv.in_points.setValue(4)
    tv.in_notes.setPlainText(notes)


def test_instructions_shared_across_versions_and_notes_persist(editor, page, tmp_path):
    tv, _, qapp, _ = editor
    t = tv.create_exam("Instr exam", [page])
    assert tv.in_instructions.isEnabled() and tv.in_instructions.toPlainText() == ""
    tv.in_instructions.setPlainText(INSTR)
    _setup_region(tv)
    assert "Notes ✓" in tv.region_list.item(0).text()
    # debounced autosave reaches the database
    wait_for(qapp, lambda: db.get_exam_instructions("Instr exam") == INSTR and not tv.dirty)
    assert db.load_template(t.id).regions[0].grading_notes == "Accept degrees or radians."

    # a copied version keeps the notes and sees the same exam instructions
    p2 = str(tmp_path / "blank_b.png"); save_image(p2, make_page(301, "VERSION B"))
    tb = tv.create_version("Instr exam", "B", [p2], copy_from=db.load_template(t.id))
    assert db.load_template(tb.id).regions[0].grading_notes == "Accept degrees or radians."
    assert tv.in_instructions.toPlainText() == INSTR

    # another exam has its own (empty) instructions; edits there don't leak
    tv.create_exam("Other exam", [page])
    assert tv.in_instructions.toPlainText() == ""
    tv.in_instructions.setPlainText("Be lenient with notation.")
    tv.flush()
    assert db.get_exam_instructions("Other exam") == "Be lenient with notation."
    assert db.get_exam_instructions("Instr exam") == INSTR


def test_rubric_generation_receives_instructions_and_notes(editor, page):
    tv, llm, qapp, mw = editor
    tv.create_exam("Rubric instr", [page])
    tv.in_instructions.setPlainText(INSTR)
    _setup_region(tv)
    tv.run_ai("rubric")       # bulk: regions without a rubric
    wait_for(qapp, lambda: tv.worker is None and bool(tv.t.regions[0].rubric))
    assert llm.rubric_calls == [{"problem": "Q1", "instructions": INSTR, "notes": "Accept degrees or radians."}]

    # changing instructions after rubrics exist shows the regenerate hint
    tv.in_instructions.setPlainText(INSTR + " Units required.")
    assert tv.lbl_instr_hint.isVisibleTo(tv) and "regenerate" in tv.lbl_instr_hint.text()
    tv.flush()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    mw.grab().save(str(SCREENSHOT_DIR / "template_instructions.png"))


def test_grader_passes_instructions_and_notes(editor, page, tmp_path):
    tv, _, qapp, _ = editor
    t = tv.create_exam("Grade instr", [page])
    tv.in_instructions.setPlainText(INSTR)
    _setup_region(tv, notes="Final answer must be boxed.")
    tv.rubric_editor.add_item(4, "both roots")
    tv.flush()

    scan = str(tmp_path / "scan.png"); save_image(scan, make_page(300, "INSTRUCTIONS"))
    subs, _ = session.create_submissions("Grade instr", [scan], 1)
    sub = subs[0]
    sub.template_id = t.id
    llm = CaptureLLM()
    Grader(llm).grade_submission(db.load_template(t.id), sub)
    assert llm.grade_calls == [{"problem": "Q1", "instructions": INSTR, "notes": "Final answer must be boxed."}]


def test_default_instructions_prefill_new_exam(editor, page, monkeypatch):
    tv, _, qapp, _ = editor
    monkeypatch.setitem(LLM_CONFIG, "default_instructions", "Show all work.")
    tv.create_exam("Prefilled exam", [page])
    assert db.get_exam_instructions("Prefilled exam") == "Show all work."
    assert tv.in_instructions.toPlainText() == "Show all work."
    # an exam that already has instructions keeps them
    db.set_exam_instructions("Kept exam", "Custom.")
    tv.create_exam("Kept exam", [page])
    assert tv.in_instructions.toPlainText() == "Custom."
