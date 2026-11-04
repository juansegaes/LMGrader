"""Sub-questions: part (b) is graded with the main question + parts (a) and (b) as context."""
import json
from lmgrader import db
from lmgrader.grading import Grader, session
from lmgrader.models import Submission, SUB_READY
from lmgrader.template.alignment import save_image
from lmgrader.template.template import (create_template_from_image, add_region, persist, duplicate_as_version,
                                        question_context, part_of, siblings)
from conftest import FakeLLM, make_page


def _template(tmp_path, name="Parts quiz"):
    p = str(tmp_path / "parts.png")
    save_image(p, make_page(71, "PARTS"))
    t = create_template_from_image(f"{name} vA", p, name, "A")
    # stored out of order on purpose: context follows the part letters, not the list order
    for lab, y, text in (("Q1b", 500, "Find the roots of $f$."), ("Q1a", 300, "Evaluate $f(0)$."),
                         ("Q2", 700, r"Solve $x^2-9=0$."), ("Q1c", 900, "Sketch $f$.")):
        r = add_region(t, lab, "g", (100, y, 300, 120), 2, rubric="- [2 pt] both roots")
        r.question_text = text
    t.stems = {"1": r"Let $f(x)=x^2-4$."}
    return t


def test_part_labels():
    assert part_of("Q1b") == ("1", "b") and part_of("1 (c)") == ("1", "c") and part_of("2.a") == ("2", "a")
    assert part_of("Q3") is None and part_of("Bonus") is None


def test_question_context(tmp_path):
    t = _template(tmp_path)
    i_a, i_b, i_2, i_c = 1, 0, 2, 3
    assert siblings(t, i_b) == [i_a, i_b, i_c]
    a = question_context(t, i_a)
    assert a.startswith("Main question 1:\nLet $f(x)=x^2-4$.") and "Earlier parts" not in a
    assert a.endswith("Part to grade: (a)\nEvaluate $f(0)$.")
    b = question_context(t, i_b)
    assert "Let $f(x)" in b and "(a) Evaluate $f(0)$." in b and b.endswith("Part to grade: (b)\nFind the roots of $f$.")
    assert "Sketch" not in b                       # later parts are not sent
    c = question_context(t, i_c)
    assert "(a) Evaluate" in c and "(b) Find the roots" in c and c.endswith("Sketch $f$.")
    assert question_context(t, i_2) == r"Solve $x^2-9=0$."   # no parts: unchanged
    t.stems = {}                                   # old templates: the stem section is skipped
    assert question_context(t, i_a).startswith("Part to grade: (a)")


def test_stems_persist_and_copy_to_versions(tmp_path):
    t = _template(tmp_path, "Parts persist")
    t.id = persist(t)
    assert db.load_template(t.id).stems == {"1": r"Let $f(x)=x^2-4$."}
    p2 = str(tmp_path / "parts_b.png")
    save_image(p2, make_page(72, "PARTS B"))
    assert duplicate_as_version(db.load_template(t.id), p2, "B").stems == {"1": r"Let $f(x)=x^2-4$."}


class ContextLLM(FakeLLM):
    def __init__(self):
        super().__init__("")
        self.questions = {}

    def grade_answer(self, b, problem, group, rubric, pts, question="", **kw):
        self.questions[problem] = question
        return {"score": 1, "feedback": "ok", "needs_human_review": False, "review_reason": "",
                "transcription": "x", "confidence": 0.95}


def test_grader_sends_context(tmp_path):
    t = _template(tmp_path, "Parts grading")
    t.id = persist(t)
    sub = Submission(exam_name=t.exam_name, status=SUB_READY, template_id=t.id,
                     scan_pages=[t.pages[0]], name_confirmed=True)
    db.add_submission(sub)
    llm = ContextLLM()
    session.grade_one(Grader(llm), sub)
    q = llm.questions
    assert "Main question 1" in q["Q1b"] and "(a) Evaluate" in q["Q1b"] and "Part to grade: (b)" in q["Q1b"]
    assert "Earlier parts" not in q["Q1a"] and q["Q2"] == r"Solve $x^2-9=0$."
    db.delete_submission(sub.id)


class NotesLLM(FakeLLM):
    """Each part's grader leaves a note; records what the later parts received."""
    def __init__(self):
        super().__init__("")
        self.calls = []

    def grade_answer(self, b, problem, group, rubric, pts, question="", prior_notes="", has_next=False, **kw):
        self.calls.append((problem, prior_notes, has_next))
        return {"score": 1, "feedback": "ok", "needs_human_review": False, "review_reason": "",
                "transcription": "x", "confidence": 0.95,
                "next_part_notes": f"{problem}: student got 7 (correct is 5)" if has_next else "ignored"}


def test_next_part_notes(tmp_path):
    """The grader of (a) tells (b) and (c) about an earlier mistake so it is not penalized twice."""
    t = _template(tmp_path, "Parts notes")
    t.id = persist(t)
    sub = Submission(exam_name=t.exam_name, status=SUB_READY, template_id=t.id,
                     scan_pages=[t.pages[0]], name_confirmed=True)
    db.add_submission(sub)
    llm = NotesLLM()
    session.grade_one(Grader(llm), sub)
    calls = {p: (notes, nxt) for p, notes, nxt in llm.calls}
    order = [p for p, _, _ in llm.calls]
    assert order.index("Q1a") < order.index("Q1b") < order.index("Q1c")   # part order, not list order
    assert calls["Q1a"] == ("", True)
    assert calls["Q1b"] == ("Part Q1a: Q1a: student got 7 (correct is 5)", True)
    assert "Part Q1a:" in calls["Q1c"][0] and "Part Q1b: Q1b:" in calls["Q1c"][0] and not calls["Q1c"][1]
    assert calls["Q2"] == ("", False)
    grades = {g.problem: g for g in db.list_grades(sub.id)}
    assert grades["Q1a"].next_part_notes.startswith("Q1a:") and grades["Q1c"].next_part_notes == ""
    # resume: (c) regraded alone still gets the stored notes of (a) and (b)
    c = db.connect(); c.execute("DELETE FROM grades WHERE submission_id=? AND problem='Q1c'", (sub.id,))
    c.commit(); c.close()
    llm.calls.clear()
    Grader(llm).grade_submission(db.load_template(t.id), db.get_submission(sub.id))
    assert [p for p, _, _ in llm.calls] == ["Q1c"] and "Part Q1b:" in llm.calls[0][1]
    db.delete_submission(sub.id)


def test_next_part_prompt():
    from lmgrader.llm.client import LLMClient, GRADE_SCHEMA, GRADE_SCHEMA_NEXT
    c = LLMClient.__new__(LLMClient)
    reply = ('{"transcription": "x", "score": 1, "feedback": "", "needs_human_review": false, '
             '"review_reason": "", "confidence": 0.9, "continues_elsewhere": false, '
             '"next_part_notes": "A^{-1} wrong"}')
    sent = []   # (prompt, schema)
    c._chat = lambda m, schema, *a, **k: (sent.append((m[1]["content"][0]["text"], schema)), reply)[1]
    r = c.grade_answer("b64", "Q1b", "g", "", 2, prior_notes="Part Q1a: inverse wrong", has_next=True)
    prompt, schema = sent[-1]
    assert r["next_part_notes"] == "A^{-1} wrong" and schema is GRADE_SCHEMA_NEXT
    assert "inverse wrong" in prompt and "do not take points off again" in prompt
    assert "next_part_notes" in prompt
    c.grade_answer("b64", "Q2", "g", "", 2)
    prompt, schema = sent[-1]
    assert schema is GRADE_SCHEMA and "next_part_notes" not in prompt
    assert "earlier parts of this question" not in prompt


def test_rubric_prompt_parts_and_true_false(tmp_path):
    """The rubric for a part sees the main question (context only); true/false is all-or-nothing."""
    from lmgrader.llm.client import LLMClient
    c = LLMClient.__new__(LLMClient)
    sent = []
    c._chat = lambda m, *a, **k: (sent.append(m[1]["content"]),
                                  '{"expected_answer": "2", "criteria": [{"description": "x", "points": 2}]}')[1]
    t = _template(tmp_path, "Parts rubric")
    c.generate_rubric(question_context(t, 0), 2, "Q1b")
    assert "Let $f(x)=x^2-4$." in sent[-1] and "Part to grade: (b)" in sent[-1]
    assert "ONLY for the part named 'Part to grade'" in sent[-1]
    assert "True/false questions get NO partial credit" in sent[-1]


class PartsLLM(FakeLLM):
    """Records which extraction each label got (the stem is only asked for the first part)."""
    def __init__(self):
        super().__init__()
        self.calls = []

    def extract_part(self, b, number, letter):
        self.calls.append(("stem+part", number + letter))
        return super().extract_part(b, number, letter)

    def extract_question(self, b, part_of=None):
        self.calls.append(("part only" if part_of else "question", "".join(part_of or ())))
        return f"Part {part_of[1]} only." if part_of else r"Solve $x^2-4=0$."


def test_split_part_reply():
    from lmgrader.llm.client import split_part_reply
    # the plain-text format used now
    r = "MAIN QUESTION:\nLet $A = \\begin{pmatrix} 1 & 2 \\\\ 0 & 1 \\end{pmatrix}$.\nPART (a):\nFind $A^{-1}$."
    d = split_part_reply(r, "a")
    assert d == {"stem": "Let $A = \\begin{pmatrix} 1 & 2 \\\\ 0 & 1 \\end{pmatrix}$.", "part": "Find $A^{-1}$."}
    assert split_part_reply("MAIN QUESTION:\nNONE\nPART (a):\nIs $W$ a subspace?") == \
        {"stem": "", "part": "Is $W$ a subspace?"}
    assert split_part_reply("**Main question:** Let $x$.\n**Part (a):** Find $y$.")["part"] == "Find $y$."
    assert split_part_reply("Plain LaTeX $x$") == {"stem": "", "part": "Plain LaTeX $x$"}
    # seen live: a stray NONE line and the part's own label
    assert split_part_reply("MAIN QUESTION:\nPART (a):\nNONE\nUse Cramer's Rule.", "a")["part"] == "Use Cramer's Rule."
    assert split_part_reply("MAIN QUESTION:\nLet $H$.\nPART (a):\n(a) $H = \\{0\\}$", "a")["part"] == "$H = \\{0\\}$"
    # older JSON replies: everything in "stem", empty "part" -> that text is the part, never raw JSON
    bad = '{\n  "stem": "The set $\\\\left\\\\{1, t, t^2, t^3\\\\right\\\\}$ is a basis for $P_2$.",\n  "part": ""\n}'
    d = split_part_reply(bad)
    assert d["stem"] == "" and d["part"].startswith("The set") and "{" not in d["part"][:1]
    assert split_part_reply('```json\n{"stem": "", "part": "Only (a)."}\n```')["part"] == "Only (a)."


def test_latex_survives_json_replies():
    """Single-backslash LaTeX in a model's JSON (\\begin, \\text, \\frac, \\sqrt, \\neq) used to be
    decoded as control characters ('egin', tab+'ext') or fail to parse at all."""
    from lmgrader.llm.client import _extract_json, repair_latex_text
    raw = r'{"transcription": "\begin{pmatrix} 1 \\ 2 \end{pmatrix}, \text{so } x \neq \frac{1}{2}, \sqrt{2}, \theta \to \infty", "score": 1}'
    d = _extract_json(raw)
    assert d["transcription"] == r"\begin{pmatrix} 1 \\ 2 \end{pmatrix}, \text{so } x \neq \frac{1}{2}, \sqrt{2}, \theta \to \infty"
    # correctly escaped JSON and real newlines are untouched
    good = json.dumps({"feedback": "Line one\nnext line \\frac{1}{2} and \"quoted\"", "x": "\u00e9"})
    assert _extract_json(good) == json.loads(good)
    # text already damaged by the old decoding is repaired
    assert repair_latex_text("Let $A=\x08egin{pmatrix} 1 \x0crac{1}{2} \text{ok}$") == \
        r"Let $A=\begin{pmatrix} 1 \frac{1}{2} \text{ok}$"
    assert repair_latex_text("Line\ne.g. a\tb") == "Line\ne.g. a\tb"


def test_looping_output_detected():
    from lmgrader.llm.client import is_repetitive, cut_repetition
    loop = ("Let $$A = \\begin{pmatrix} 1 & 2 & 1 \\\\ 0 & 1 & 1 \\\\ 1 & 2 & 2 \\text{ (The matrix is clearly "
            "defined with a closing bracket)} \\text{ Let's use the LaTeX representation of the visible math.} ") * 4
    assert is_repetitive(loop)
    assert len(cut_repetition(loop)) < len(loop) / 3
    assert not is_repetitive(r"Let $A = \begin{pmatrix} 1 & 1 & 0 \\ 0 & 1 & 2 \\ 1 & 1 & 1 \end{pmatrix}$. "
                             r"(a) Use the Gauss-Jordan algorithm on $[A \mid I_3]$ to compute $A^{-1}$.")


class LoopLLM:
    """LLMClient whose model loops on the first request and behaves on the retry."""
    def __init__(self, replies):
        from lmgrader.llm.client import LLMClient
        self.c = LLMClient.__new__(LLMClient)
        self.replies, self.prompts = list(replies), []
        self.c._chat = lambda messages, *a, **k: (self.prompts.append(messages[0]["content"][0]["text"]),
                                                  self.replies.pop(0))[1]


def test_extract_part_retries_when_looping():
    loop = ("MAIN QUESTION:\n" + "Let $A$ be the matrix. (Note: revising based on visual confirmation.) " * 5
            + "\nPART (a):\nFind $A^{-1}$.")
    ok = "MAIN QUESTION:\nLet $A = I_2$.\nPART (a):\nFind $A^{-1}$."
    f = LoopLLM([loop, ok])
    assert f.c.extract_part("b64", "2", "a") == {"stem": "Let $A = I_2$.", "part": "Find $A^{-1}$."}
    assert len(f.prompts) == 2 and "exactly once" in f.prompts[1]
    f = LoopLLM([loop, loop])                   # still looping: keep the text before the repetition
    d = f.c.extract_part("b64", "2", "a")
    assert d["stem"].count("revising") <= 1 or len(d["stem"]) < 200


def test_prepare_crop_sizes():
    import numpy as np
    from lmgrader.llm.client import prepare_crop, MAX_IMAGE_PIXELS
    big = prepare_crop(np.zeros((1400, 2200, 3), np.uint8))          # used to be sent at full size
    assert max(big.shape[:2]) <= 1600 and big.shape[0] * big.shape[1] <= MAX_IMAGE_PIXELS
    small = prepare_crop(np.zeros((60, 300, 3), np.uint8))           # still upscaled to be readable
    assert min(small.shape[:2]) > 60
    wide = prepare_crop(np.zeros((244, 1131, 3), np.uint8))
    assert max(wide.shape[:2]) <= 1600


def test_repair_raw_json_in_question_boxes(tmp_path):
    from lmgrader.template.template import repair_part_json, question_context
    t = _template(tmp_path, "Parts repair")
    t.stems = {}
    t.regions[1].question_text = '{"stem": "Let $f(x)=x^2-4$.", "part": "Evaluate $f(0)$."}'   # Q1a
    t.regions[0].question_text = '{\n  "stem": "Find the roots of $f$.",\n  "part": ""\n}'      # Q1b
    assert "Find the roots" in question_context(t, 0) and '"stem"' not in question_context(t, 0)
    assert repair_part_json(t)
    assert t.regions[1].question_text == "Evaluate $f(0)$." and t.stems == {"1": "Let $f(x)=x^2-4$."}
    assert t.regions[0].question_text == "Find the roots of $f$."     # not taken as a stem
    assert not repair_part_json(t)
    # damaged LaTeX is repaired, looping text is cleared so it gets extracted again
    t.stems["1"] = "Let $A=\x08egin{pmatrix} 1 \\end{pmatrix}$, \x0crac12 \text{ok}"
    t.regions[2].question_text = "The matrix is clearly defined with a closing bracket here. " * 4
    changes = repair_part_json(t)
    assert t.stems["1"] == "Let $A=\\begin{pmatrix} 1 \\end{pmatrix}$, \\frac12 \\text{ok}"
    assert t.regions[2].question_text == "" and any("cleared" in c for c in changes)


def test_extraction_splits_stem(qapp, tmp_path, monkeypatch):
    """Only the first part is asked for the main question; later parts get their own text only.
    The stem is stored once and never overwritten."""
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    from test_template_editor import wait_for
    llm = PartsLLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        t = _template(tmp_path, "Parts extract")
        for r in t.regions:
            r.question_text = ""
        t.stems = {}
        t.id = persist(t)
        tv.refresh_list(t.id); tv.load(t.id)
        tv.run_ai("extract")
        wait_for(qapp, lambda: tv.worker is None)
        assert sorted(llm.calls) == sorted([("stem+part", "1a"), ("part only", "1b"), ("part only", "1c"),
                                            ("question", "")])
        assert tv.t.stems == {"1": "Let $f(x)=x^2-4$ (question 1)."}
        assert tv.t.regions[1].question_text == "Part a: solve $f(x)=0$."
        assert tv.t.regions[0].question_text == "Part b only."
        assert tv.t.regions[2].question_text == r"Solve $x^2-4=0$."   # no parts: plain extraction
        # the selected part shows the shared main question
        tv.region_list.setCurrentRow(0); qapp.processEvents()
        assert tv.stem_box.isVisibleTo(tv) and "Q1a, Q1b, Q1c" in tv.lbl_stem.text()
        assert "main question + part (a) + part (b)" in tv.lbl_stem_ctx.text()
        from conftest import SCREENSHOT_DIR
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        mw.grab().save(str(SCREENSHOT_DIR / "template_editor_subquestions.png"))
        tv.in_stem.setPlainText("Edited stem.")
        tv.flush()
        assert db.load_template(t.id).stems["1"] == "Edited stem."
        # re-extracting the first part keeps the edited stem
        tv.t.regions[1].question_text = ""
        tv.run_ai("extract")
        wait_for(qapp, lambda: tv.worker is None)
        assert tv.t.stems["1"] == "Edited stem."
        tv.region_list.setCurrentRow(2); qapp.processEvents()
        assert not tv.stem_box.isVisibleTo(tv)
    finally:
        tv.flush(); mw.close()
