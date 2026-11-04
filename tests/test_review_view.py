"""Review page: only graded tests listed, multi-page buttons, persisted edits, next-flagged navigation."""
import pytest
from lmgrader import db
from lmgrader.grading import session, Grader
from lmgrader.models import Student, SUB_GRADED, SUB_READY
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

EXAM = "ReviewExam"

class FlaggingLLM(FakeLLM):
    """Flags every answer for human review."""
    def grade_answer(self, *a, **kw):
        res = super().grade_answer(*a, **kw)
        res.update(needs_human_review=True, review_reason="illegible")
        return res

@pytest.fixture(scope="module")
def review_world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("review")
    p1, p2 = str(tmp / "t1.png"), str(tmp / "t2.png")
    save_image(p1, make_page(21, "REVIEW ONE")); save_image(p2, make_page(22, "REVIEW TWO"))
    t = create_template_from_image(f"{EXAM} vA", [p1, p2], EXAM, "A")
    for page, lab in ((0, "Q1"), (1, "Q2")):
        r = add_region(t, lab, "g", (100, 300, 300, 150), 4, rubric="- [4 pt] both roots")
        r.page, r.question_text = page, r"Solve $x^2-4=0$."
    t.id = persist(t)
    sids = {}
    for n in ("Rev Alpha", "Rev Beta", "Rev Gamma", "Rev Delta"):
        sids[n] = db.add_student(Student(name=n))

    def scans(k):
        out = []
        for i, (seed, txt) in enumerate([(21, "REVIEW ONE"), (22, "REVIEW TWO")]):
            sp = str(tmp / f"s{k}_{i}.png"); save_image(sp, make_page(seed, txt)); out.append(sp)
        return out

    pages = scans(0) + scans(1) + scans(2) + scans(3)
    subs, left = session.create_submissions(EXAM, pages, 2, source="review.pdf")
    assert len(subs) == 4 and not left
    for sub, name in zip(subs, sids):
        sub.template_id = t.id
        session.confirm_student(sub, sids[name])
    clean, flagged, partial, new = subs
    session.grade_one(Grader(FakeLLM()), clean)
    session.grade_one(Grader(FlaggingLLM()), flagged)
    done = []
    session.grade_one(Grader(FakeLLM()), partial, progress_cb=lambda i, n: done.append(i),
                      should_stop=lambda: len(done) >= 1)
    assert db.get_submission(clean.id).status == SUB_GRADED
    assert db.get_submission(partial.id).status == SUB_READY and len(db.list_grades(partial.id)) == 1
    assert not db.list_grades(new.id)
    return {"template": t, "clean": clean, "flagged": flagged, "partial": partial, "new": new}

def _view(qapp):
    from lmgrader.ui.review_view import ReviewView
    v = ReviewView()
    v.resize(1300, 850)
    v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findData(EXAM))
    qapp.processEvents()
    return v

def _row_of(v, sub_id):
    return next(i for i, (s, _) in enumerate(v._rows) if s.id == sub_id)

def test_only_graded_listed(qapp, review_world):
    v = _view(qapp)
    ids = {s.id for s, _ in v._rows}
    w = review_world
    assert w["clean"].id in ids and w["flagged"].id in ids and w["partial"].id in ids
    assert w["new"].id not in ids
    status = v.table.item(_row_of(v, w["partial"].id), 3).text()
    assert status == "Partially graded"
    assert v.table.item(_row_of(v, w["flagged"].id), 3).text() == "Needs human grade (2)"
    assert v.table.item(_row_of(v, w["clean"].id), 3).text() == "Done"

def test_page_buttons_and_page_tag(qapp, review_world):
    g = db.list_grades(review_world["clean"].id)[0]
    g.next_part_notes = "Student's x = 3 is wrong (correct: x = 2); later parts reuse it."
    db.save_grade(g)
    v = _view(qapp)
    v.table.selectRow(_row_of(v, review_world["clean"].id)); qapp.processEvents()
    assert [b.text() for b in v.page_buttons] == ["Page 1", "Page 2"]
    assert len(v.grade_cards) == 2
    # Q2 is on page 2 -> carries a "p2" pill; Q1 has none
    from PyQt6.QtWidgets import QLabel
    texts = lambda c: [l.text() for l in c.findChildren(QLabel)]
    assert "p2" in texts(v.grade_cards[1]) and "p2" not in texts(v.grade_cards[0])
    assert any("Note for later parts" in s for s in texts(v.grade_cards[0]))   # the grader's note is shown
    for _ in range(3):   # let thumbnails rescale and the layout settle before grabbing
        qapp.processEvents()
    v.grab().save(str(SCREENSHOT_DIR / "review.png"))

def test_score_edit_and_mark_reviewed_persist(qapp, review_world):
    v = _view(qapp)
    sid = review_world["flagged"].id
    v.table.selectRow(_row_of(v, sid)); qapp.processEvents()
    card = v.grade_cards[0]
    assert card.banner.isVisibleTo(card)
    # typing a score is a human grade: flag resolved and persisted
    card.spin.setValue(1.5); qapp.processEvents()
    g = db.list_grades(sid)[0]
    assert g.score == 1.5 and g.human_graded and not g.needs_human_review
    assert not card.banner.isVisibleTo(card)
    # Q2 is still an AI suggestion -> total is provisional (Q2 stays flagged for test_next_flagged)
    assert v.table.item(_row_of(v, sid), 2).text() == "4.5 / 8 *"   # 1.5 (edited) + 3 (Q2)

def test_class_average(qapp, review_world):
    """Average of the fully graded tests of the exam (the partial one is left out); it follows
    score edits and ignores the Show filter."""
    w = review_world
    v = _view(qapp)
    v.cb_filter.setCurrentText("Needs human grade"); qapp.processEvents()
    v.table.selectRow(_row_of(v, w["flagged"].id)); qapp.processEvents()
    totals = [sum(g.score for g in db.list_grades(w[k].id)) for k in ("clean", "flagged")]
    s, m, n, same = v.class_average(v._current)
    assert n == 2 and m == 8 and same and abs(s - sum(totals) / 2) < 1e-9
    assert v.lbl_avg.text() == f"Class average {s:.1f} / 8 · {100 * s / 8:.0f}%"
    card = v.grade_cards[1]
    card.spin.setValue(card.g.score - 1); qapp.processEvents()     # live update
    assert v.class_average(v._current)[0] == pytest.approx(s - 0.5)
    assert f"{s - 0.5:.1f} / 8" in v.lbl_avg.text()
    card.spin.setValue(card.g.score + 1); qapp.processEvents()     # leave the fixture as it was
    card.g.human_graded, card.g.needs_human_review = False, True
    db.save_grade(card.g)

def test_next_flagged(qapp, review_world):
    v = _view(qapp)
    flagged = [i for i, (s, g) in enumerate(v._rows) if any(x.needs_human_review for x in g)]
    assert flagged, "fixture should leave at least one flagged grade"
    v.table.clearSelection()
    r = v.next_flagged(); qapp.processEvents()
    assert r == flagged[0] and v._selected_row() == r
    r2 = v.next_flagged()
    assert r2 == (flagged[1] if len(flagged) > 1 else flagged[0])   # wraps around
    # resolve everything -> nothing left to jump to
    for sub, grades in v._rows:
        for g in grades:
            if g.needs_human_review:
                g.needs_human_review = False
                db.save_grade(g)
    v.load_submissions()
    assert v.next_flagged() == -1

def test_latex_html_renders_math():
    from lmgrader.ui.mathtext import latex_html
    h = latex_html("- [2 pt] roots $x = \\pm 2$\n- [1 pt] <check> & $$\\frac{a}{b}$$ costs $5", "#1f2433")
    assert h.count("<img") == 2 and "\\pm" not in h and "\\frac" not in h
    assert "&lt;check&gt; &amp;" in h and "$5" in h          # plain text escaped; money left alone
    assert "<br>" in h

class _RegradeLLM(FakeLLM):
    def __init__(self, score=4.0, error=False):
        super().__init__()
        self.score, self.error = score, error
    def grade_answer(self, *a, **kw):
        res = super().grade_answer(*a, **kw)
        if self.error:
            res.update(score=0.0, feedback="LLM error: down", needs_human_review=True, review_reason="llm_error")
        else:
            res.update(score=self.score, feedback="Regraded: full marks.", confidence=0.97)
        return res

def _retry(qapp, v, card, llm, monkeypatch):
    import time
    import lmgrader.ui.review_view as rv
    monkeypatch.setattr(rv.LLMClient, "from_config", staticmethod(lambda: llm))
    card.btn_retry.click()
    end = time.time() + 20
    while v.retry_worker is not None and time.time() < end:
        qapp.processEvents(); time.sleep(0.01)
    qapp.processEvents()
    assert v.retry_worker is None

def test_rubric_and_retry_on_every_question(qapp, review_world, monkeypatch):
    import lmgrader.ui.review_view as rv
    sid = review_world["clean"].id
    v = _view(qapp)
    v.table.selectRow(_row_of(v, sid)); qapp.processEvents()
    card = v.grade_cards[0]
    assert not card.g.needs_human_review
    # not flagged: rubric hidden until asked for
    assert not card.rubric_box.isVisibleTo(card) and "both roots" in card.lbl_rubric.text()
    card.btn_rubric.click(); assert card.rubric_box.isVisibleTo(card)
    before = db.list_grades(sid)[0]
    # an LLM error keeps the current grade
    warned = []
    monkeypatch.setattr(rv.QMessageBox, "warning", staticmethod(lambda *a: warned.append(a)))
    _retry(qapp, v, card, _RegradeLLM(error=True), monkeypatch)
    assert warned and db.list_grades(sid)[0].score == before.score
    # success: replaces score + feedback in place (same row id), card rebuilt with the new grade
    card = v.grade_cards[0]
    _retry(qapp, v, card, _RegradeLLM(4.0), monkeypatch)
    after = db.list_grades(sid)[0]
    assert after.id == before.id and after.score == 4.0 and after.feedback == "Regraded: full marks."
    assert v.grade_cards[0].g.score == 4.0 and v.grade_cards[0].spin.value() == 4.0
    assert len(db.list_grades(sid)) == 2
    # a human grade is only replaced after confirming
    card = v.grade_cards[0]
    card.spin.setValue(2.0); qapp.processEvents()
    monkeypatch.setattr(rv.QMessageBox, "question", staticmethod(lambda *a: rv.QMessageBox.StandardButton.No))
    card.btn_retry.click(); qapp.processEvents()
    assert v.retry_worker is None and db.list_grades(sid)[0].score == 2.0
    monkeypatch.setattr(rv.QMessageBox, "question", staticmethod(lambda *a: rv.QMessageBox.StandardButton.Yes))
    _retry(qapp, v, card, _RegradeLLM(3.5), monkeypatch)
    g = db.list_grades(sid)[0]
    assert g.score == 3.5 and not g.human_graded
