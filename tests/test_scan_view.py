"""Scan & Grade page: upload -> split -> names -> grade, all persisted (offscreen, fake LLM)."""
import os
import time

import pytest

from conftest import FakeLLM, make_page, SCREENSHOT_DIR
from lmgrader import db
from lmgrader.models import SUB_IDENTIFIED, SUB_READY, SUB_GRADED, SUB_GRADING
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from lmgrader.llm.client import LLMClient

EXAM = "Quiz 2P"


class SeqLLM(FakeLLM):
    """Reads names from a list in call order; can stop the grading worker on a given call."""
    def __init__(self, names, view=None, stop_after=None):
        super().__init__("")
        self.names, self.view, self.stop_after, self.graded = list(names), view, stop_after, []

    def read_name(self, b):
        return {"name": self.names.pop(0) if self.names else "", "legible": True}

    def grade_answer(self, b, problem, group, rubric, pts, question="", **kw):
        self.graded.append(problem)
        if self.stop_after and len(self.graded) == self.stop_after and self.view.worker is not None:
            self.view.worker.cancelled = True   # user presses Stop mid-test
        return super().grade_answer(b, problem, group, rubric, pts, question)


def wait_idle(app, view, timeout=60):
    t0 = time.time()
    while view.worker is not None and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.03)
    app.processEvents()
    assert view.worker is None, "worker did not finish"


@pytest.fixture(scope="module")
def exam2p(world, data_dir):
    """2-page exam: name + Q1 on page 1, Q2 on page 2; plus scans of two students' tests."""
    d = data_dir / "quiz2p"
    d.mkdir(exist_ok=True)
    p1, p2 = str(d / "t1.png"), str(d / "t2.png")
    save_image(p1, make_page(31, "QUIZ PAGE 1"))
    save_image(p2, make_page(32, "QUIZ PAGE 2"))
    t = create_template_from_image("Quiz 2P vA", [p1, p2], EXAM, "A")
    for page, lab in ((0, "Q1"), (1, "Q2")):
        r = add_region(t, lab, "algebra", (100, 300, 300, 150), 2, rubric="- [2 pt] both roots")
        r.page, r.question_text = page, r"Solve $x^2-4=0$."
    t.name_rect, t.name_page = (500, 100, 300, 60), 0
    t.id = persist(t)
    scans = []
    for i, (seed, txt) in enumerate([(31, "QUIZ PAGE 1"), (32, "QUIZ PAGE 2")] * 2):
        sp = str(d / f"scan_{i}.png")
        save_image(sp, make_page(seed, txt))
        scans.append(sp)
    return {"template_id": t.id, "scans": scans}


@pytest.fixture
def view(qapp, exam2p, monkeypatch):
    from lmgrader.ui.scan_view import ScanView
    v = ScanView()
    v.resize(1300, 820)
    v.show()
    v.refresh()
    v.cb_exam.setCurrentIndex(v.cb_exam.findText(EXAM))
    qapp.processEvents()
    yield v
    v.shutdown()
    v.close()


def _use_llm(monkeypatch, llm):
    monkeypatch.setattr(LLMClient, "from_config", classmethod(lambda cls: llm))


def test_upload_split_names_grade_resume(qapp, view, exam2p, monkeypatch):
    llm = SeqLLM(["Bob Smith", "zzz qqq"], view=view)
    _use_llm(monkeypatch, llm)
    assert view.pages_per_test() == 2

    # step 1: one long "file" with two students' 2-page tests
    entries = [{"path": p, "file": "batch.pdf", "page": i + 1} for i, p in enumerate(exam2p["scans"])]
    ids = view.add_pages(entries)
    assert len(ids) == 2
    wait_idle(qapp, view)
    subs = {s.id: s for s in db.list_submissions_by_status(EXAM)}
    assert [len(subs[i].pages) for i in ids] == [2, 2]
    assert subs[ids[0]].source == "batch.pdf p1–2" and subs[ids[1]].source == "batch.pdf p3–4"

    # step 2: first name matched confidently, second needs the human
    assert subs[ids[0]].status == SUB_READY and subs[ids[0]].name_confirmed
    assert subs[ids[1]].status == SUB_IDENTIFIED and not subs[ids[1]].name_confirmed
    assert os.path.exists(os.path.join(os.environ["LMGRADER_DATA"], "name_crops", f"sub_{ids[0]}.png"))
    assert "1 need you" in view.step_btns[1].text()
    # needs-me rows are sorted first in the names table
    assert view.table_names.item(0, 0).data(0x0100) == ids[1]
    view.step_group.button(1).click(); qapp.processEvents()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    view.grab().save(str(SCREENSHOT_DIR / "scan_names.png"))

    alice = next(s for s in db.list_students() if s.name == "Alice Johnson")
    from lmgrader.ui.scan_view import COL_N_STUDENT
    combo = view.table_names.cellWidget(0, COL_N_STUDENT)
    combo.setCurrentIndex(combo.findData(alice.id))
    qapp.processEvents()
    s1 = db.get_submission(ids[1])
    assert s1.student_id == alice.id and s1.name_confirmed and s1.status == SUB_READY

    # step 3: stop after the first question, then resume — nothing graded twice
    llm.stop_after = 1
    view.grade_all()
    wait_idle(qapp, view)
    assert llm.graded == ["Q1"]
    assert db.get_submission(ids[0]).status == SUB_READY and len(db.list_grades(ids[0])) == 1
    assert view.banner.isVisible()
    llm.stop_after = None
    view.grade_all()
    wait_idle(qapp, view)
    assert llm.graded == ["Q1", "Q2", "Q1", "Q2"]
    assert all(db.get_submission(i).status == SUB_GRADED for i in ids)
    view.step_group.button(2).click(); qapp.processEvents()
    view.grab().save(str(SCREENSHOT_DIR / "scan_grade.png"))

    # reopening the page (e.g. after restarting the app) shows the same persisted state
    from lmgrader.ui.scan_view import ScanView
    v2 = ScanView()
    v2.cb_exam.setCurrentIndex(v2.cb_exam.findText(EXAM))
    assert sorted(s.id for s in v2.subs) == sorted(ids)
    assert v2.table_grade.rowCount() == 2 and "2/2 graded" in v2.step_btns[2].text()
    v2.close()


def test_leftover_pages_are_kept_for_next_upload(qapp, view, exam2p, monkeypatch):
    _use_llm(monkeypatch, SeqLLM([]))
    view.chk_names.setChecked(False)
    before = len(view.subs)
    scans = exam2p["scans"]
    ids = view.add_pages([{"path": scans[0], "file": "a.pdf", "page": 1},
                          {"path": scans[1], "file": "a.pdf", "page": 2},
                          {"path": scans[2], "file": "b.pdf", "page": 1}])
    wait_idle(qapp, view)
    assert len(ids) == 1 and view.pending_box.isVisible() and "b.pdf p1" in view.lbl_pending.text()
    view.step_group.button(0).click(); qapp.processEvents()
    view.grab().save(str(SCREENSHOT_DIR / "scan_upload.png"))
    ids2 = view.add_pages([{"path": scans[3], "file": "b.pdf", "page": 2}])
    wait_idle(qapp, view)
    assert len(ids2) == 1 and not view.pending_box.isVisible()
    assert db.get_submission(ids2[0]).source == "b.pdf p1–2"
    assert len(view.subs) == before + 2
    for i in ids + ids2:
        db.delete_submission(i)


def test_regrade_graded_tests(qapp, view, exam2p, monkeypatch):
    """Regrade: AI grades are replaced (hand-graded questions kept unless told otherwise), the
    back-page-reviewed mark is cleared and the tests are graded again right away."""
    from lmgrader.grading import session
    llm = SeqLLM([])
    _use_llm(monkeypatch, llm)
    view.chk_names.setChecked(False)
    scans = exam2p["scans"]
    ids = view.add_pages([{"path": p, "file": "rg.pdf", "page": i + 1} for i, p in enumerate(scans)])
    wait_idle(qapp, view)
    bob = next(s for s in db.list_students() if s.name == "Bob Smith")
    for i in ids:
        session.confirm_student(db.get_submission(i), bob.id)
    view.reload()
    view.grade_all(); wait_idle(qapp, view)
    assert all(db.get_submission(i).status == SUB_GRADED for i in ids)
    a, b = ids
    # a: Q1 graded by hand; back page marked reviewed
    g = db.list_grades(a)[0]
    g.score, g.human_graded, g.feedback = 0.5, True, "mine"
    db.save_grade(g)
    s = db.get_submission(a); s.back_reviewed = True; db.update_submission(s)
    view.reload()
    assert view.btn_regrade.isEnabled() and view._regradable(db.get_submission(a))
    llm.graded.clear()
    view.regrade([a, b], confirm=False, keep_human=True)
    assert view.worker is not None and db.get_submission(a).status == SUB_READY
    wait_idle(qapp, view)
    assert sorted(llm.graded) == ["Q1", "Q2", "Q2"]                  # a's Q1 (hand-graded) skipped
    ga = {x.problem: x for x in db.list_grades(a)}
    assert ga["Q1"].human_graded and ga["Q1"].score == 0.5 and ga["Q1"].feedback == "mine"
    assert all(db.get_submission(i).status == SUB_GRADED for i in ids)
    assert not db.get_submission(a).back_reviewed
    # not keeping hand grades: everything is graded again
    llm.graded.clear()
    view.regrade([a], confirm=False, keep_human=False)
    wait_idle(qapp, view)
    assert sorted(llm.graded) == ["Q1", "Q2"] and not any(x.human_graded for x in db.list_grades(a))
    for i in ids:
        db.delete_submission(i)


class SlowLLM(SeqLLM):
    """Blocks inside a 'request' until aborted, like a real LLM call that Stop interrupts."""
    def __init__(self, names):
        super().__init__(names)
        import threading
        self.aborted = threading.Event()
        self.started = threading.Event()

    def abort(self):
        self.aborted.set()

    def read_name(self, b):
        self.started.set()
        if not self.aborted.wait(20):
            return super().read_name(b)
        raise ConnectionError("request aborted")


def test_stop_identification_keeps_untouched_tests_waiting(qapp, view, exam2p, monkeypatch):
    from lmgrader.models import SUB_NEW
    llm = SlowLLM(["Bob Smith"])
    _use_llm(monkeypatch, llm)
    view.chk_names.setChecked(True)
    scans = exam2p["scans"]
    ids = view.add_pages([{"path": p, "file": "s.pdf", "page": i + 1} for i, p in enumerate(scans)])
    assert view.worker is not None and view.btn_stop_job.isVisible()
    t0 = time.time()
    while not llm.started.is_set() and time.time() - t0 < 20:
        qapp.processEvents(); time.sleep(0.02)
    view.stop()                      # aborts the in-flight request immediately
    wait_idle(qapp, view, timeout=10)
    assert llm.aborted.is_set()
    assert [db.get_submission(i).status for i in ids] == [SUB_NEW, SUB_NEW]
    assert "Stopped" in view.lbl_progress.text()
    for i in ids:
        db.delete_submission(i)


def test_double_sided_layout_split(qapp, view, exam2p, monkeypatch):
    """A template with back sides: 2 template pages -> 4 scanned pages per test."""
    from lmgrader.models import BLANK
    _use_llm(monkeypatch, SeqLLM([]))
    view.chk_names.setChecked(False)
    t = db.load_template(exam2p["template_id"])
    t.scan_layout = [0, BLANK, 1, BLANK]
    db.save_template(t)
    try:
        view.refresh()
        assert view.scan_pages_per_test() == 4 and "4 scanned pages per test (2 pages + 2 backs)" in view.lbl_pages.text()
        s = exam2p["scans"]
        pages = [s[0], "back1.png", s[1], "back2.png", s[2], "back3.png", s[3], "back4.png"]
        ids = view.add_pages([{"path": p, "file": "duplex.pdf", "page": i + 1} for i, p in enumerate(pages)])
        wait_idle(qapp, view)
        assert len(ids) == 2
        a, b = db.get_submission(ids[0]), db.get_submission(ids[1])
        assert a.pages == [s[0], s[1]] and a.back_pages == ["back1.png", "back2.png"]
        assert b.pages == [s[2], s[3]] and b.back_pages == ["back3.png", "back4.png"]
        assert a.source == "duplex.pdf p1–4"
        for i in ids:
            db.delete_submission(i)
    finally:
        t.scan_layout = []
        db.save_template(t)
        view.refresh()


def test_main_window_recovers_interrupted_grading(qapp, exam2p, monkeypatch):
    _use_llm(monkeypatch, SeqLLM([]))
    from lmgrader.models import Submission
    sub = Submission(exam_name=EXAM, status=SUB_GRADING, template_id=exam2p["template_id"],
                     scan_pages=exam2p["scans"][:2], name_confirmed=True)
    db.add_submission(sub)
    from lmgrader.ui.main_window import MainWindow
    mw = MainWindow()
    assert mw.recovered >= 1 and "Recovered" in mw.statusBar().currentMessage()
    assert db.get_submission(sub.id).status == SUB_READY
    mw.template_view.dirty = False
    mw.close()
    db.delete_submission(sub.id)
