"""Annotated feedback PDFs: red scores/feedback on the scanned pages, summary page, index.csv."""
import csv
import numpy as np
import pytest
from lmgrader import db
from lmgrader.export import feedback as fb
from lmgrader.grading import session, Grader
from lmgrader.models import Student, SUB_GRADED
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

EXAM = "Feedback Exam/2026"   # slash on purpose: file names must be sanitized

class WordyLLM(FakeLLM):
    def grade_answer(self, *a, **kw):
        res = super().grade_answer(*a, **kw)
        res["feedback"] = ("Correct factoring and both roots found. Remember to state the final answer "
                           r"clearly as a set, e.g. $x \in \{-2, 2\}$ since $x^2 = 4$, and show the check step.")
        return res

@pytest.fixture(scope="module")
def fb_world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fb")
    p1, p2 = str(tmp / "t1.png"), str(tmp / "t2.png")
    save_image(p1, make_page(31, "FEEDBACK ONE")); save_image(p2, make_page(32, "FEEDBACK TWO"))
    t = create_template_from_image("fb vA", [p1, p2], EXAM, "A")
    for page, lab, rect in ((0, "Q1", (100, 300, 300, 150)),      # room on the right
                            (1, "Q2", (60, 250, 730, 160)),       # full width: text goes below
                            (1, "Q3", (60, 820, 730, 200))):      # full width near the bottom
        r = add_region(t, lab, "g", rect, 4, rubric="- [4 pt] both roots")
        r.page, r.question_text = page, r"Solve $x^2-4=0$."
    t.id = persist(t)
    sid = db.add_student(Student(name="Fé Ñandú", student_id="A/123", email="fe@example.edu"))
    pages = []
    for k in range(2):
        for i, (seed, txt) in enumerate([(31, "FEEDBACK ONE"), (32, "FEEDBACK TWO")]):
            sp = str(tmp / f"s{k}_{i}.png"); save_image(sp, make_page(seed, txt)); pages.append(sp)
    subs, _ = session.create_submissions(EXAM, pages, 2)
    a, b = subs
    a.template_id = b.template_id = t.id
    session.confirm_student(a, sid)
    session.confirm_student(b, None)          # unassigned
    session.grade_one(Grader(WordyLLM()), a)
    session.grade_one(Grader(FakeLLM()), b)
    assert db.get_submission(a.id).status == SUB_GRADED
    return {"t": t, "a": db.get_submission(a.id), "b": db.get_submission(b.id), "tmp": tmp, "sid": sid}

def _student(sid):
    return next(s for s in db.list_students() if s.id == sid)

def test_render_pages_red_annotations(fb_world):
    w = fb_world
    pages = fb.render_feedback_pages(w["a"], w["t"], db.list_grades(w["a"].id), _student(w["sid"]))
    assert len(pages) == 3                       # summary + 2 scanned pages
    p1 = np.asarray(pages[1])
    red = (p1[:, :, 0] > 170) & (p1[:, :, 1] < 70) & (p1[:, :, 2] < 70)
    x, y, ww, hh = w["t"].regions[0].rect
    assert red[y - 40:y + hh + 5, x - 5:x + ww + 5].sum() > 200            # outline + score
    assert red[y:y + hh, x + ww + 10:].sum() == 0                           # no feedback on the scan
    assert red[:60, 600:].sum() > 50                                        # total header on page 1
    p0 = np.asarray(pages[0].convert("L"))
    assert (p0 < 128).sum() > 2000                                          # feedback on the summary
    pages[1].save(SCREENSHOT_DIR / "feedback_page.png")
    pages[2].save(SCREENSHOT_DIR / "feedback_page2.png")
    pages[0].save(SCREENSHOT_DIR / "feedback_summary.png")

def test_summary_fits_one_page(fb_world):
    """Many questions with long feedback: the text shrinks so everything stays on one page;
    only an absurd amount of text spills onto a second page (never dropped)."""
    from lmgrader.models import Grade
    long = ("You factored correctly, but the final answer must list both roots and the check "
            r"step $(\pm 2)^2 - 4 = 0$ is missing; also state the solution set clearly. ")
    rows = [Grade(problem=f"Q{i}", score=2, max_points=4, feedback=long, human_graded=True) for i in range(20)]
    pages = fb._summary_pages((1700, 2200), "E", "A", None, fb_world["a"], rows)
    assert len(pages) == 1
    pages[0].save(SCREENSHOT_DIR / "feedback_summary_many.png")
    rows = [Grade(problem=f"Q{i}", score=2, max_points=4, feedback=long * 4, human_graded=True) for i in range(40)]
    assert len(fb._summary_pages((1700, 2200), "E", "A", None, fb_world["a"], rows)) > 1

def test_latex_to_text():
    assert fb.latex_to_text(r"$x^2 \pm 1$") == "x² ± 1"
    assert fb.latex_to_text(r"$\frac{1}{2}$ and $x \in \{a\}$, $a_1 \le \infty$") == "(1)/(2) and x ∈ {a}, a₁ ≤ ∞"

def _kinds(text):
    return [i[0] if i[0] != "w" else i[1] for i in fb._rich_items(text, fb._font(20, text=True), fb.RED)]

def test_feedback_math_rendered():
    pytest.importorskip("matplotlib")
    assert _kinds(r"Use $\frac{1}{2}$ and \(x \le y \implies z\).") == ["Use", "m", "and", "m", "."]
    assert _kinds(r"Then $$\int_0^1 x\,dx$$ done") == ["Then", "br", "m", "br", "done"]
    assert _kinds("Costs $5 and $6 here.") == ["Costs", "$5", "and", "$6", "here."]       # money stays text
    assert _kinds(r"Bad $\begin{cases}1\end{cases}$") == ["Bad", r"\begin{cases}1\end{cases}"]  # fallback
    rich = fb._layout_rich(r"Long $\frac{x^2+1}{2}$ text " * 6, fb._font(20, text=True), 200, 4)
    assert len(rich.lines) > 1 and rich.w <= 200

def test_internal_errors_not_shown(fb_world):
    g = db.list_grades(fb_world["a"].id)[0]
    g.feedback, g.review_reason = "LLM error: boom", "llm_error"
    assert fb._shown_feedback(g) == ""

def test_export_submission_pdf(fb_world):
    import pypdfium2 as pdfium
    out = str(fb_world["tmp"] / "one.pdf")
    fb.export_submission_pdf(fb_world["a"], out)
    pdf = pdfium.PdfDocument(out)
    assert len(pdf) == 3
    pdf.close()
    fb.export_submission_pdf(fb_world["a"], out, summary=False)
    pdf = pdfium.PdfDocument(out)
    assert len(pdf) == 2
    pdf.close()

def test_export_exam_and_index(fb_world):
    out_dir = fb_world["tmp"] / "export"
    res = fb.export_exam(EXAM, str(out_dir))
    names = sorted(p.name for p in out_dir.glob("*.pdf"))
    assert names == sorted(["A_123_Feedback_Exam_2026.pdf", f"unassigned_{fb_world['b'].id}.pdf"])
    assert len(res) == 2
    with open(out_dir / "index.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if r["student_id"] == "A/123")
    assert row["email"] == "fe@example.edu" and row["total"] == "9" and row["max"] == "12"
    assert row["pdf"] == "A_123_Feedback_Exam_2026.pdf"

def test_review_view_feedback_edit_persists(qapp, fb_world):
    from lmgrader.ui.review_view import ReviewView
    v = ReviewView(); v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findData(EXAM)); qapp.processEvents()
    row = next(i for i, (s, _) in enumerate(v._rows) if s.id == fb_world["a"].id)
    v.table.selectRow(row); qapp.processEvents()
    card = v.grade_cards[0]
    card.fb_edit.setPlainText("Nice work.")
    card.flush()
    assert db.list_grades(fb_world["a"].id)[0].feedback == "Nice work."
    img = v.render_preview_page()
    assert img is not None and img.size[0] > 100
