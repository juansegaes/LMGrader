"""Structured rubrics, question reordering, and hand grading by groups of answers."""
import time

import pytest

from conftest import FakeLLM, make_page
from lmgrader import db
from lmgrader.models import (Rubric, RubricItem, AnswerRegion, Submission, Student, parse_rubric_text,
                             SCORING_NEGATIVE, SUB_READY, SUB_GRADED, SUB_IDENTIFIED)
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from lmgrader.grading import hand, session
from lmgrader.grading.grader import Grader


# ---------- rubric model ----------
def test_rubric_scoring_add_up_and_deduct():
    a, b, c = RubricItem(2, "setup"), RubricItem(2, "answer"), RubricItem(-1, "sign slip")
    pos = Rubric(max_points=4, items=[a, b, c])
    assert pos.score([]) == 0 and pos.score([a.id, b.id]) == 4 and pos.score([a.id, c.id]) == 1
    assert pos.score([c.id]) == 0                       # never below 0
    assert pos.score([a.id, b.id, "gone"]) == 4         # deleted items are ignored
    neg = Rubric(max_points=4, scoring=SCORING_NEGATIVE, items=[RubricItem(-1, "x"), RubricItem(-5, "y")])
    assert neg.score([]) == 4 and neg.score([neg.items[0].id]) == 3 and neg.score([it.id for it in neg.items]) == 0
    assert pos.feedback([a.id, c.id], "Nice work") == "+2  setup\n-1  sign slip\nNice work"
    assert not Rubric() and Rubric(items=[a]) and Rubric(expected_answer="2")


def test_rubric_text_for_the_llm_and_json_roundtrip():
    r = Rubric(max_points=3, expected_answer="x=2", items=[RubricItem(1, "a"), RubricItem(2, "b")])
    assert r.as_text() == "Expected answer: x=2\n\nCriteria:\n- [1 pt] a\n- [2 pt] b"
    neg = Rubric(max_points=3, scoring=SCORING_NEGATIVE, items=[RubricItem(-1, "slip")])
    assert "start from 3 points" in neg.as_text() and "- [-1 pt] slip" in neg.as_text()
    back = Rubric.from_obj(r.to_dict(), 3)
    assert back == r and back.items[0].id == r.items[0].id
    assert Rubric.from_obj(None, 2).is_empty()


def test_legacy_text_rubrics_become_items():
    old = ("Expected answer: $x=\\pm 2$\n\nCriteria:\n- [1 pt] factors\n- [3 pt] both roots\n"
           "(Note: criteria sum to 4, cap the score at 4.)")
    r = parse_rubric_text(old, 4)
    assert r.expected_answer == "$x=\\pm 2$" and [(i.points, i.description) for i in r.items] == \
        [(1, "factors"), (3, "both roots")]
    assert [(i.points, i.description) for i in parse_rubric_text("2 pts: setup\n(1) answer", 3).items] == \
        [(2, "setup"), (1, "answer")]
    free = parse_rubric_text("Full credit for a correct derivative.", 5)
    assert len(free.items) == 1 and free.items[0].points == 5
    assert parse_rubric_text("- [-1 pt] slip\n- [-2 pt] wrong sign", 4).scoring == SCORING_NEGATIVE
    # constructing a region with text (older code / answer keys) gives a structured rubric
    reg = AnswerRegion(problem="Q1", points=4, rubric="- [4 pt] both roots")
    assert isinstance(reg.rubric, Rubric) and reg.rubric.items[0].points == 4


# ---------- fixtures: one version, two questions, three tests ----------
@pytest.fixture(scope="module")
def hexam(world, data_dir):
    d = data_dir / "hand"
    d.mkdir(exist_ok=True)
    p = str(d / "t.png")
    save_image(p, make_page(41, "HAND QUIZ"))
    t = create_template_from_image("Hand vA", p, "Hand quiz", "A")
    q1 = add_region(t, "Q1", "g", (100, 300, 300, 150), 4)
    q1.rubric = Rubric(max_points=4, items=[RubricItem(2, "factors"), RubricItem(2, "both roots")])
    q2 = add_region(t, "Q2", "g", (100, 600, 300, 150), 2, rubric="- [2 pt] all correct")
    q1.question_text = q2.question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    subs = []
    for k in range(3):
        sp = str(d / f"s{k}.png")
        save_image(sp, make_page(41, "HAND QUIZ"))
        s = Submission(exam_name="Hand quiz", template_id=t.id, status=SUB_READY, scan_pages=[sp],
                       name_confirmed=True, student_id=k + 1)
        db.add_submission(s)
        subs.append(s.id)
    return {"tid": t.id, "subs": subs}


def test_reordering_questions_moves_their_grades(hexam):
    from lmgrader.models import Grade
    tid, sid = hexam["tid"], hexam["subs"][0]
    t = db.load_template(tid)
    extra = add_region(t, "Q3", "g", (500, 300, 200, 100), 1)
    persist(t)
    t = db.load_template(tid)
    for i, r in enumerate(t.regions):
        db.save_grade(Grade(submission_id=sid, region_id=i, problem=r.problem, score=i, max_points=r.points))
    t.regions = [t.regions[2], t.regions[0], t.regions[1]]          # Q3, Q1, Q2
    persist(t)
    by_region = {g.region_id: g.problem for g in db.list_grades(sid)}
    assert by_region == {0: "Q3", 1: "Q1", 2: "Q2"}
    del t.regions[0]                                                 # delete Q3: its grade goes too
    persist(t)
    assert {g.region_id: g.problem for g in db.list_grades(sid)} == {0: "Q1", 1: "Q2"}
    assert [r.problem for r in db.load_template(tid).regions] == ["Q1", "Q2"]
    db.delete_grades(sid)


def test_hand_grading_groups_and_individual_answers(hexam):
    t = db.load_template(hexam["tid"])
    a, b, c = hexam["subs"]
    answers = hand.prepare_answers(t, 0, detect_blanks=True)
    assert [x.sub.id for x in answers] == [a, b, c] and all(x.crop for x in answers)
    assert all(0 <= x.p_not_blank <= 1 for x in answers)
    uid = t.regions[0].uid
    gid = db.add_hand_group(t.id, uid, "Group 1", [a, b])
    g = next(g for g in hand.groups(t, 0) if g["id"] == gid)
    items = [t.regions[0].rubric.items[0].id]
    hand.grade_group(t, 0, g, items, "Factor first.")
    graded = hand.hand_graded(t, 0)
    assert set(graded) == {a, b} and all(x.score == 2 and x.source == "hand" for x in graded.values())
    assert graded[a].feedback == "+2  factors\nFactor first." and graded[a].hand_group_id == gid
    assert db.list_hand_groups(t.id, uid)[0]["items"] == items       # the group remembers the choice
    # moving an answer to another group takes it out of the first one
    gid2 = db.add_hand_group(t.id, uid, "Group 2", [b])
    assert {g["id"]: g["members"] for g in hand.groups(t, 0)} == {gid: [a], gid2: [b]}
    # an ungrouped answer is graded on its own; replaces an AI grade
    from lmgrader.models import Grade
    db.save_grade(Grade(submission_id=c, region_id=0, problem="Q1", score=1, max_points=4, feedback="AI"))
    hand.save_hand_grade(t, 0, c, [it.id for it in t.regions[0].rubric.items])
    rows = [x for x in db.list_grades(c) if x.region_id == 0]
    assert len(rows) == 1 and rows[0].score == 4 and rows[0].human_graded
    # every question of c graded -> the test is graded; deleting a hand grade puts it back
    assert db.get_submission(c).status == SUB_READY
    hand.save_hand_grade(t, 1, c, [t.regions[1].rubric.items[0].id])
    assert db.get_submission(c).status == SUB_GRADED
    db.delete_grade(rows[0].id)
    session.refresh_status(c)
    assert db.get_submission(c).status == SUB_READY


def test_llm_never_regrades_hand_grades(hexam):
    t = db.load_template(hexam["tid"])
    a = hexam["subs"][0]
    db.delete_grades(a)
    hand.save_hand_grade(t, 0, a, [t.regions[0].rubric.items[1].id], "by hand")

    class Recorder(FakeLLM):
        def __init__(self):
            super().__init__()
            self.asked = []

        def grade_answer(self, b, problem, group, rubric, pts, question="", **kw):
            self.asked.append(problem)
            return {"score": 0, "feedback": "ai", "needs_human_review": False, "review_reason": "",
                    "transcription": "", "confidence": 1.0}

    llm = Recorder()
    grades = Grader(llm).grade_submission(t, db.get_submission(a))
    assert llm.asked == ["Q2"]                       # Q1 was graded by hand: skipped
    q1 = next(g for g in grades if g.region_id == 0)
    assert q1.source == "hand" and q1.feedback == "+2  both roots\nby hand"

    # a hand grade saved while the LLM is answering wins too
    db.delete_grades(a)

    class Racer(Recorder):
        def grade_answer(self, b, problem, group, rubric, pts, question="", **kw):
            if problem == "Q1":
                hand.save_hand_grade(t, 0, a, [], "human was faster")
            return super().grade_answer(b, problem, group, rubric, pts, question, **kw)

    Grader(Racer()).grade_submission(t, db.get_submission(a))
    q1 = [g for g in db.list_grades(a) if g.region_id == 0]
    assert len(q1) == 1 and q1[0].comment == "human was faster"


# ---------- UI ----------
def _wait(qapp, cond, timeout=30):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert cond()


def test_rubric_editor_add_delete_reorder(qapp):
    from lmgrader.ui.rubric_editor import RubricEditor
    ed = RubricEditor()
    ed.set_rubric(Rubric(max_points=3), 3)
    seen = []
    ed.changed.connect(lambda: seen.append(1))
    ed.add_item(1, "setup"); ed.add_item(2, "answer"); ed.add_item(-1, "slip")
    assert [i.description for i in ed.rubric().items] == ["setup", "answer", "slip"] and seen
    ed.move_item(2, 0)
    assert [i.description for i in ed.rubric().items] == ["slip", "setup", "answer"]
    ed.table.selectRow(0); ed.move_selected(1)
    assert [i.description for i in ed.rubric().items] == ["setup", "slip", "answer"]
    ed.delete_item(1)
    assert [i.points for i in ed.rubric().items] == [1, 2]
    assert "3 / 3" in ed.lbl_total.text()
    ed.cb_scoring.setCurrentIndex(1)
    assert ed.rubric().scoring == SCORING_NEGATIVE


def test_rubric_editor_deduct_shows_the_points_and_is_remembered(qapp):
    import json
    from lmgrader.config import UI_CONFIG, settings_path
    from lmgrader.ui.rubric_editor import RubricEditor
    ed = RubricEditor()
    ed.set_rubric(Rubric(max_points=4), 4)
    assert ed.cb_scoring.itemText(1) == "Deduct (from 4)" and ed.rubric().scoring == "positive"
    ed.set_max_points(2.5)
    assert ed.cb_scoring.itemText(1) == "Deduct (from 2.5)"
    ed.cb_scoring.setCurrentIndex(1)                       # the professor picks Deduct...
    assert UI_CONFIG["rubric_scoring"] == SCORING_NEGATIVE
    assert json.loads(settings_path().read_text(encoding="utf-8"))["ui"]["rubric_scoring"] == SCORING_NEGATIVE
    # ...so the next empty rubric starts with Deduct (a new editor too: Hand Grade's Edit rubric…)
    for e in (ed, RubricEditor()):
        e.set_rubric(Rubric(max_points=3), 3)
        assert e.cb_scoring.currentData() == SCORING_NEGATIVE and e.rubric().scoring == SCORING_NEGATIVE
        assert e.cb_scoring.itemText(1) == "Deduct (from 3)"
        assert e.add_item() == 0 and e.rubric().items[0].points == -1
    # a rubric with items keeps its own scoring, and showing it changes nothing
    ed.set_rubric(Rubric(max_points=2, items=[RubricItem(2, "right")]), 2)
    assert ed.cb_scoring.currentData() == "positive" and UI_CONFIG["rubric_scoring"] == SCORING_NEGATIVE
    ed.set_rubric(Rubric(), 0)
    assert ed.cb_scoring.itemText(1) == "Deduct (from max)"      # no points set yet


def test_next_question_starts_with_the_scoring_picked_last(qapp, tmp_path):
    from lmgrader.ui.main_window import MainWindow
    p = str(tmp_path / "scoring_page.png")
    save_image(p, make_page(83, "SCORING"))
    mw = MainWindow(); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        tv.create_exam("Scoring memory", [p])
        tv.on_rect_drawn((100, 300, 300, 150))
        tv.on_rect_drawn((100, 600, 300, 150))
        tv.region_list.setCurrentRow(0)
        tv.in_points.setValue(5)
        ed = tv.rubric_editor
        assert ed.cb_scoring.itemText(1) == "Deduct (from 5)"
        ed.cb_scoring.setCurrentIndex(1)
        assert tv.t.regions[0].rubric.scoring == SCORING_NEGATIVE
        tv.region_list.setCurrentRow(1)                    # Q2: 1 point, no rubric yet
        assert ed.cb_scoring.currentData() == SCORING_NEGATIVE and ed.cb_scoring.itemText(1) == "Deduct (from 1)"
        ed.add_item(-0.5, "sign slip")
        assert tv.t.regions[1].rubric.scoring == SCORING_NEGATIVE
        tv.flush()
        assert db.load_template(tv.t.id).regions[1].rubric.negative
    finally:
        tv.flush(); mw.close()


def test_hand_grade_view_blanks_group_grade(qapp, hexam, monkeypatch):
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_BLANKS, PAGE_GROUP, PAGE_GRADE
    t = db.load_template(hexam["tid"])
    for s in hexam["subs"]:
        db.delete_grades(s)
    uid = t.regions[1].uid
    for g in db.list_hand_groups(t.id, uid):
        db.delete_hand_group(g["id"])
    # the CNN calls two answers blank
    a, b, c = hexam["subs"]
    real = hand.prepare_answers

    def fake_prepare(template, i, detect_blanks=False, progress=None, should_stop=None):
        out = real(template, i, False, progress, should_stop)
        if detect_blanks:
            for x in out:
                x.p_not_blank = 0.1 if x.sub.id in (a, b) else 0.9
        return out
    monkeypatch.setattr(hand, "prepare_answers", fake_prepare)
    asked = []
    monkeypatch.setattr(group_setup, "ask_kind", lambda *a, **k: asked.append(a[1]) or "blank")

    v = HandGradeView()
    v.resize(1300, 850); v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findText("Hand quiz"))
    v.cb_question.setCurrentIndex(v.cb_question.findData(uid))
    v.open_question()
    _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == PAGE_BLANKS)
    assert asked == ["Q2"] and db.get_hand_setup(t.id, uid)[0] == "blank"
    assert v.grid_blanks.count() == 2
    v._toggle_blank(v.grid_blanks.item(1))            # b is not blank after all
    v.confirm_blanks()
    assert v.stack.currentIndex() == PAGE_GROUP
    groups = db.list_hand_groups(t.id, uid)
    assert len(groups) == 1 and groups[0]["is_blank"] and groups[0]["members"] == [a]
    assert v.grid_group.count() == 2                    # ungrouped: b, c

    v._set_stage("grade")
    assert v.stack.currentIndex() == PAGE_GRADE and v.list_queue.count() == 3   # Blank group, b, c
    assert v.cur == ("group", groups[0]["id"])
    v.save_current()                                     # blank: nothing checked -> 0
    assert v.cur == ("answer", b)
    v.toggle_item(0)
    assert v.lbl_score.text() == "2 / 2"
    # the rubric is bigger than the rest of the app while grading
    from lmgrader.ui.hand_grade_view import RUBRIC_PT
    row = v.item_rows[0]
    assert RUBRIC_PT > 10 and row.desc.font().pointSize() == RUBRIC_PT
    assert row.desc.font().pointSize() > v.in_comment.font().pointSize()
    qapp.processEvents()
    from conftest import SCREENSHOT_DIR
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    v.grab().save(str(SCREENSHOT_DIR / "hand_grade_rubric.png"))
    v.in_comment.setPlainText("Well done")
    v.save_current()
    assert v.cur == ("answer", c)
    told = _finished_message(monkeypatch)
    v.save_current()
    assert "That was the last question" in told[0]
    graded = hand.hand_graded(t, 1)
    assert {s: g.score for s, g in graded.items()} == {a: 0, b: 2, c: 0}
    assert graded[b].comment == "Well done"
    assert "3/3 by hand" in v.cb_question.currentText()
    v.close()


def _finished_message(monkeypatch, press=None, screenshot=None):
    """Stands in for the user at the "question finished" message: returns the list of texts shown
    and presses the button whose text starts with `press` (None = the first button)."""
    from PyQt6.QtWidgets import QApplication, QMessageBox
    told = []

    def answer(box):
        told.append(box.text())
        if screenshot:
            from conftest import SCREENSHOT_DIR
            box.show()
            QApplication.processEvents()
            box.grab().save(str(SCREENSHOT_DIR / screenshot))
        next(b for b in box.buttons() if press is None or b.text().startswith(press)).click()
    monkeypatch.setattr(QMessageBox, "exec", answer)
    return told


def _fresh_question(t, i):
    """Question i of the hexam version as if never opened: no grades, groups or setup."""
    for s in db.list_submissions(template_id=t.id):
        for g in db.list_grades(s.id):
            if g.region_id == i:
                db.delete_grade(g.id)
        session.refresh_status(s.id, t)
    for g in db.list_hand_groups(t.id, t.regions[i].uid):
        db.delete_hand_group(g["id"])
    db.set_hand_stage(t.id, t.regions[i].uid, "start")


def _open(qapp, v, uid, stage_page):
    v.cb_exam.setCurrentIndex(v.cb_exam.findText("Hand quiz"))
    v.cb_question.setCurrentIndex(v.cb_question.findData(uid))
    v.open_question()
    _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == stage_page)


def test_blank_group_always_there_and_selection_toggles(qapp, hexam, monkeypatch):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QAbstractItemView
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP, PAGE_GRADE
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="blank")
    db.set_hand_stage(t.id, uid, "group")              # (blanks skipped: "Don't sort blanks")
    a, b, c = hexam["subs"]
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    _open(qapp, v, uid, PAGE_GROUP)
    groups = db.list_hand_groups(t.id, uid)
    assert len(groups) == 1 and groups[0]["is_blank"] and groups[0]["members"] == []
    assert v.list_groups.count() == 1 and "Blank" in v.list_groups.item(0).text()
    # an empty group is not graded
    v._set_stage("grade")
    assert v.stack.currentIndex() == PAGE_GRADE and [k for k, _ in v.queue] == ["answer"] * 3
    v._set_stage("group")
    # a click toggles one answer, as Ctrl-click would (no modifier held)
    grid = v.grid_group
    assert grid.selectionMode() == QAbstractItemView.SelectionMode.MultiSelection
    for row in (0, 2):
        rect = grid.visualItemRect(grid.item(row))
        QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    assert sorted(grid.selected_ids()) == [a, c]
    rect = grid.visualItemRect(grid.item(2))
    QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    assert grid.selected_ids() == [a]
    QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    from conftest import SCREENSHOT_DIR
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    v.grab().save(str(SCREENSHOT_DIR / "hand_group_toggle.png"))
    # Add selection to group ▾ lists the Blank group; adding puts the answers in it
    v._fill_add_menu()
    assert [act.text() for act in v.menu_add.actions()] == ["Blank  (0 answers)"]
    v.add_selection_to(groups[0]["id"])
    assert db.list_hand_groups(t.id, uid)[0]["members"] == [a, c]
    assert v.grid_group.count() == 1                    # ungrouped: b
    # taking every answer out (or "Empty the Blank group") keeps the group
    db.remove_from_hand_groups(t.id, uid, [a])
    v.list_groups.setCurrentRow(0)
    assert v.btn_delete_group.text() == "Empty the Blank group"
    v.dissolve_group()
    groups = db.list_hand_groups(t.id, uid)
    assert len(groups) == 1 and groups[0]["is_blank"] and groups[0]["members"] == []
    v.close()


def test_click_on_a_group_adds_the_selected_answers(qapp, hexam):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP, ROLE_SUB
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="blank")
    db.set_hand_stage(t.id, uid, "group")
    c = hexam["subs"][2]
    db.add_hand_group(t.id, uid, "Right", [c])
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        _open(qapp, v, uid, PAGE_GROUP)
        grid, groups = v.grid_group, v.list_groups
        left = lambda w, rect: QTest.mouseClick(w.viewport(), Qt.MouseButton.LeftButton,
                                                Qt.KeyboardModifier.NoModifier, rect.center())
        right_row = lambda: next(r for r in range(groups.count()) if groups.item(r).text().startswith("Right"))
        right = lambda: next(g for g in db.list_hand_groups(t.id, uid) if g["label"] == "Right")
        assert v.cb_filter.currentData() == "ungrouped" and grid.count() == 2
        first = grid.item(0).data(ROLE_SUB)
        left(grid, grid.visualItemRect(grid.item(0)))                     # select one answer...
        left(groups, groups.visualItemRect(groups.item(right_row())))    # ...and click the group
        assert sorted(right()["members"]) == sorted([first, c])
        assert v.cb_filter.currentData() == "ungrouped" and grid.count() == 1 and not grid.selected_ids()
        # nothing selected: a click on the group shows its answers
        left(groups, groups.visualItemRect(groups.item(right_row())))
        assert v.cb_filter.currentData() == right()["id"] and grid.count() == 2
        assert sorted(right()["members"]) == sorted([first, c])
    finally:
        v.close()


def test_confirm_groups_shows_every_group_and_moves_answers_out(qapp, hexam):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP, PAGE_CONFIRM, PAGE_GRADE
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="short")
    db.set_hand_stage(t.id, uid, "group")
    a, b, c = hexam["subs"]
    blank = db.add_hand_group(t.id, uid, hand.BLANK_LABEL, [a], is_blank=True)
    right = db.add_hand_group(t.id, uid, "x = 2", [b, c], auto_key="text:x=2", checked=False)
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        _open(qapp, v, uid, PAGE_GROUP)
        assert [b.text() for b in v.stage_btns][2:] == ["3  Confirm groups", "4  Grade"]
        v.btn_to_confirm.click()
        qapp.processEvents()
        assert v.stack.currentIndex() == PAGE_CONFIRM and v.stage_btns[2].isChecked()
        assert db.get_hand_stage(t.id, uid) == "confirm"
        # one section per group, every answer shown, no scrolling inside a group
        grids = v.confirm_grids
        ids = lambda g: sorted(g.item(r).data(Qt.ItemDataRole.UserRole) for r in range(g.count()))
        assert [ids(g) for g in grids] == [[a], sorted([b, c])]
        for g in grids:
            assert g.verticalScrollBar().maximum() == 0
            assert g.visualItemRect(g.item(g.count() - 1)).bottom() <= g.viewport().height()
        assert "⚑" in v.lbl_confirm_info.text() and not v.btn_confirm_ungroup.isEnabled()
        from conftest import SCREENSHOT_DIR
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        v.grab().save(str(SCREENSHOT_DIR / "hand_confirm_groups.png"))
        # c doesn't belong: select it (a click toggles), move it to ungrouped
        g2 = grids[1]
        row_c = next(r for r in range(g2.count()) if g2.item(r).data(Qt.ItemDataRole.UserRole) == c)
        QTest.mouseClick(g2.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         g2.visualItemRect(g2.item(row_c)).center())
        assert g2.selected_ids() == [c] and v.btn_confirm_ungroup.text() == "Move 1 selected to ungrouped"
        v.btn_confirm_ungroup.click()
        members = {g["id"]: g["members"] for g in db.list_hand_groups(t.id, uid)}
        assert members == {blank: [a], right: [b]}
        assert len(v.confirm_grids) == 2 and v.confirm_grids[1].count() == 1
        assert "1 answer is not in a group" in v.lbl_confirm_info.text()
        # Groups are right: the suggested group counts as checked, then grading
        v.btn_confirm_ok.click()
        assert all(g["checked"] for g in db.list_hand_groups(t.id, uid))
        assert v.stack.currentIndex() == PAGE_GRADE and db.get_hand_stage(t.id, uid) == "grade"
        assert v.queue == [("group", blank), ("group", right), ("answer", c)]
        # the stage button goes back to it
        v.stage_btns[2].click()
        assert v.stack.currentIndex() == PAGE_CONFIRM
    finally:
        v.close()


def test_dissolving_a_group_ungrades_its_answers(qapp, hexam, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="blank")
    db.set_hand_stage(t.id, uid, "group")
    a, b, c = hexam["subs"]
    item = t.regions[0].rubric.items[0].id
    blank = db.add_hand_group(t.id, uid, hand.BLANK_LABEL, [a], is_blank=True)
    right = db.add_hand_group(t.id, uid, "Right", [b])
    hand.grade_group(t, 0, next(g for g in hand.groups(t, 0) if g["id"] == blank), [], "")
    hand.grade_group(t, 0, next(g for g in hand.groups(t, 0) if g["id"] == right), [item], "ok")
    hand.save_hand_grade(t, 0, c, [item])                      # an ungrouped answer graded on its own
    assert set(hand.hand_graded(t, 0)) == {a, b, c}
    asked = []
    answer = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(QMessageBox, "question", lambda *x, **k: asked.append(x[2]) or answer[0])
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        _open(qapp, v, uid, PAGE_GROUP)
        row = lambda name: next(r for r in range(v.list_groups.count())
                                if v.list_groups.item(r).text().startswith(name))
        v.list_groups.setCurrentRow(row("Right"))
        v.dissolve_group()                                     # asked first; Cancel keeps everything
        assert "1 hand grade is deleted" in asked[0] and set(hand.hand_graded(t, 0)) == {a, b, c}
        answer[0] = QMessageBox.StandardButton.Yes
        v.dissolve_group()
        assert set(hand.hand_graded(t, 0)) == {a, c}             # b is ungraded, c untouched
        assert [g["id"] for g in hand.groups(t, 0)] == [blank]
        assert "2/3 by hand" in v.cb_question.currentText()
        v.list_groups.setCurrentRow(row("Blank"))
        v.dissolve_group()                                     # Empty the Blank group: same
        assert set(hand.hand_graded(t, 0)) == {c}
        g = hand.groups(t, 0)
        assert len(g) == 1 and g[0]["is_blank"] and g[0]["members"] == [] and g[0]["items"] == []
        # a group with no grades is dissolved without asking
        db.add_hand_group(t.id, uid, "Other", [a, b])
        v._render_group()
        v.list_groups.setCurrentRow(row("Other"))
        n_asked = len(asked)
        v.dissolve_group()
        assert len(asked) == n_asked and len(hand.groups(t, 0)) == 1
    finally:
        v.close()


def test_regrade_from_scratch_keeps_the_question_type(qapp, hexam, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GRADE, PAGE_BLANKS
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="blank")
    db.set_hand_stage(t.id, uid, "grade")
    a, b, c = hexam["subs"]
    item = t.regions[0].rubric.items[0].id
    gid = db.add_hand_group(t.id, uid, "Right", [a, b])
    hand.grade_group(t, 0, next(g for g in hand.groups(t, 0) if g["id"] == gid), [item])
    hand.save_hand_grade(t, 0, c, [item])
    real = hand.prepare_answers

    def fake_prepare(template, i, detect_blanks=False, progress=None, should_stop=None):
        out = real(template, i, False, progress, should_stop)
        for x in out:
            x.p_not_blank = (0.1 if x.sub.id == a else 0.9) if detect_blanks else None
        return out
    monkeypatch.setattr(hand, "prepare_answers", fake_prepare)
    monkeypatch.setattr(group_setup, "ask_kind", lambda *x, **k: pytest.fail("the type is kept, not asked"))
    answer = [QMessageBox.StandardButton.Cancel]
    msgs = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *x, **k: msgs.append(x[2]) or answer[0])
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        _open(qapp, v, uid, PAGE_GRADE)
        assert v.btn_regrade.isEnabled()
        v.btn_regrade.click()                                  # Cancel: nothing deleted
        assert "1 group and 3 hand grades will be deleted" in msgs[0]
        assert len(hand.hand_graded(t, 0)) == 3
        answer[0] = QMessageBox.StandardButton.Yes
        v.btn_regrade.click()
        _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == PAGE_BLANKS)
        assert hand.hand_graded(t, 0) == {} and db.get_hand_setup(t.id, uid)[0] == "blank"
        assert [g["is_blank"] and not g["members"] for g in hand.groups(t, 0)] == [True]
        assert db.get_hand_stage(t.id, uid) == "blanks" and v.grid_blanks.count() == 1   # a looks blank
        assert "0/3 by hand" in v.cb_question.currentText()
    finally:
        v.close()


def test_save_and_next_opens_the_next_question(qapp, hexam, monkeypatch):
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GRADE, PAGE_BLANKS
    t = db.load_template(hexam["tid"])
    q1, q2 = t.regions[0].uid, t.regions[1].uid
    _fresh_question(t, 0)
    _fresh_question(t, 1)
    db.set_hand_setup(t.id, q1, kind="blank")
    db.set_hand_stage(t.id, q1, "grade")
    asked = []
    monkeypatch.setattr(group_setup, "ask_kind", lambda *a, **k: asked.append(a[1]) or "blank")
    told = _finished_message(monkeypatch, press="Go to", screenshot="hand_question_finished.png")
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    _open(qapp, v, q1, PAGE_GRADE)
    assert len(v.queue) == 3
    for _ in range(3):
        assert v.cb_question.currentData() == q1 and told == []       # nothing said while answers are left
        v.save_current()
    # Q1 is graded: the user is told, then Save & next opens Q2 (its category is asked the first time)
    assert told == ["Every answer to Q1 is graded by hand.\n\nNext is Q2."]
    assert v.cb_question.currentData() == q2 and asked == ["Q2"]
    _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == PAGE_BLANKS)
    assert len(hand.hand_graded(t, 0)) == 3
    v.close()


def test_question_finished_message_can_stay_on_the_question(qapp, hexam, monkeypatch):
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GRADE
    t = db.load_template(hexam["tid"])
    q1 = t.regions[0].uid
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, q1, kind="blank")
    db.set_hand_stage(t.id, q1, "grade")
    asked = []
    monkeypatch.setattr(group_setup, "ask_kind", lambda *a, **k: asked.append(a[1]) or "blank")
    told = _finished_message(monkeypatch, press="Stay on Q1")
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        _open(qapp, v, q1, PAGE_GRADE)
        for _ in range(3):
            v.save_current()
        assert len(told) == 1 and asked == []
        assert v.cb_question.currentData() == q1 and v.stack.currentIndex() == PAGE_GRADE
        assert "3/3 by hand" in v.cb_question.currentText()
    finally:
        v.close()


@pytest.mark.parametrize("key, modifier, stays", [("Key_Return", "NoModifier", False),
                                                  ("Key_Return", "ControlModifier", False),
                                                  ("Key_Escape", "NoModifier", True)])
def test_question_finished_message_keys(qapp, hexam, monkeypatch, key, modifier, stays):
    """The real modal message, answered from the keyboard."""
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GRADE
    t = db.load_template(hexam["tid"])
    q1, q2 = t.regions[0].uid, t.regions[1].uid
    _fresh_question(t, 0)
    _fresh_question(t, 1)
    db.set_hand_setup(t.id, q1, kind="blank")
    db.set_hand_stage(t.id, q1, "grade")
    monkeypatch.setattr(group_setup, "ask_kind", lambda *a, **k: None)
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    left_open = []

    def press():
        box = QApplication.activeModalWidget()
        QTest.keyClick(box.focusWidget(), getattr(Qt.Key, key), getattr(Qt.KeyboardModifier, modifier))
        if box.isVisible():                 # never leave the test waiting on a modal box
            left_open.append(box.text())
            box.reject()
    try:
        _open(qapp, v, q1, PAGE_GRADE)
        for _ in range(2):
            v.save_current()
        QTimer.singleShot(0, press)
        v.save_current()
        assert left_open == []
        assert v.cb_question.currentData() == (q1 if stays else q2)
    finally:
        v.close()


def test_change_question_type_starts_over(qapp, hexam, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from lmgrader.models import Grade
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP, PAGE_BLANKS
    t = db.load_template(hexam["tid"])
    uid = t.regions[0].uid
    a, b, c = hexam["subs"]
    _fresh_question(t, 0)
    db.set_hand_setup(t.id, uid, kind="blank")
    db.set_hand_stage(t.id, uid, "group")
    gid = db.add_hand_group(t.id, uid, "Group 1", [a, b])
    hand.grade_group(t, 0, next(g for g in hand.groups(t, 0) if g["id"] == gid), [t.regions[0].rubric.items[0].id])
    db.save_grade(Grade(submission_id=c, region_id=0, problem="Q1", score=3, max_points=4, feedback="AI, reviewed",
                        human_graded=True))                       # set in Review: stays
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    _open(qapp, v, uid, PAGE_GROUP)
    assert v.btn_change_kind.isEnabled()
    warned = []
    answer = {"warn": QMessageBox.StandardButton.Cancel, "kind": "short"}
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]) or answer["warn"])
    monkeypatch.setattr(group_setup, "ask_kind", lambda *a, **k: answer["kind"])
    # cancelled at the warning, or at the type: nothing deleted
    v.change_kind()
    assert "can't be undone" in warned[0] and "1 group and 2 hand grades" in warned[0]
    answer.update(warn=QMessageBox.StandardButton.Yes, kind=None)
    v.change_kind()
    assert len(hand.hand_graded(t, 0)) == 3 and db.get_hand_setup(t.id, uid)[0] == "blank"
    # confirmed: groups and hand grades go, the Review grade stays, the new type starts
    answer["kind"] = "blank"
    v.change_kind()
    _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == PAGE_BLANKS)
    left = hand.hand_graded(t, 0)
    assert list(left) == [c] and left[c].feedback == "AI, reviewed"
    assert all(not g["members"] for g in db.list_hand_groups(t.id, uid))
    assert db.get_hand_stage(t.id, uid) == "blanks" and db.get_hand_setup(t.id, uid)[0] == "blank"
    assert db.get_submission(a).status != SUB_GRADED
    v.close()
