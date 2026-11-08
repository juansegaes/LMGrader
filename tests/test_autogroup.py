"""Answer groups suggested without an LLM: multiple choice marks (OMR against the blank exam) and
short answers read with TrOCR (faked here)."""
import time

import cv2
import numpy as np
import pytest

from conftest import make_page
from lmgrader import db
from lmgrader.config import CROP_PADDING
from lmgrader.grading import autogroup, hand, marks
from lmgrader.models import Rubric, RubricItem, Submission, SUB_READY
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist

FONT = cv2.FONT_HERSHEY_SIMPLEX
BUBBLES = [(130, 330 + 55 * k) for k in range(4)]      # centers; labels 12, 13, 15, 17 on their right
MC_RECT = (100, 300, 300, 240)
TEXT_RECT = (100, 660, 440, 70)                         # "A) 12   B) 15   C) 17" (circle the answer)
SHORT_RECT = (560, 300, 260, 90)                        # "Answer:" printed in the box


def mc_page(seed=7):
    img = make_page(seed, "MC QUIZ")
    cv2.rectangle(img, (80, 250), (840, 760), (255, 255, 255), -1)       # a clean question area
    cv2.putText(img, "1. Pick one:", (90, 280), FONT, 0.8, (0, 0, 0), 2)
    for (cx, cy), lab in zip(BUBBLES, ("12", "13", "15", "17")):
        cv2.circle(img, (cx, cy), 15, (0, 0, 0), 2)
        cv2.putText(img, lab, (160, cy + 10), FONT, 0.9, (0, 0, 0), 2)
    for x, lab in ((120, "A) 12"), (270, "B) 15"), (420, "C) 17")):
        cv2.putText(img, lab, (x, 705), FONT, 0.8, (0, 0, 0), 2)
    cv2.putText(img, "Answer:", (570, 335), FONT, 0.7, (0, 0, 0), 2)
    return img


def filled(img, *ks):
    out = img.copy()
    for k in ks:
        cv2.circle(out, BUBBLES[k], 12, (30, 30, 30), -1)
    return out


# ---------- marks (pure image functions) ----------
def test_bubbles_are_found_with_their_labels():
    page = mc_page()
    boxes = marks.detect_choices(page, MC_RECT)
    assert len(boxes) == 4 and marks.has_bubbles(page, MC_RECT)
    for (x, y, w, h), (cx, cy) in zip(boxes, BUBBLES):
        assert x <= cx - 15 and cx + 15 <= x + w and y <= cy - 15 and cy + 15 <= y + h
        assert x + w >= 190                                  # the printed label is in the box
    for a, b in zip(boxes, boxes[1:]):                       # stacked boxes meet, never overlap
        assert a[1] + a[3] <= b[1]
    assert not marks.has_bubbles(page, TEXT_RECT)


def test_marked_choice_blank_two_marks_and_a_small_shift():
    page = mc_page()
    boxes = marks.detect_choices(page, MC_RECT)
    pick = lambda scan: marks.decide([marks.mark_score(scan, page, b) for b in boxes])
    assert pick(page) == []
    assert pick(filled(page, 2)) == [2]
    assert pick(filled(page, 0, 3)) == [0, 3]
    shifted = np.roll(filled(page, 1), (2, -2), axis=(0, 1))   # alignment is never perfect
    assert pick(shifted) == [1]
    tick = page.copy()                                          # a tick through the second bubble
    cv2.line(tick, (116, 383), (128, 398), (0, 0, 0), 3); cv2.line(tick, (128, 398), (156, 362), (0, 0, 0), 3)
    assert pick(tick) == [1]


def test_decide_rules():
    assert marks.decide([0.15, 0.0, 0.004]) == [0]
    assert marks.decide([0.01, 0.0]) == []                   # noise only: blank
    assert marks.decide([0.15, 0.14]) == [0, 1]              # two marks
    assert marks.decide([0.15, 0.05]) is None                # an erased / crossed-out mark: unsure
    assert marks.decide([]) is None


def test_circle_the_answer_without_bubbles():
    page = mc_page()
    boxes = marks.detect_choices(page, TEXT_RECT)
    assert len(boxes) == 3 and [b[0] for b in boxes] == sorted(b[0] for b in boxes)
    scan = page.copy()
    cv2.ellipse(scan, (305, 695), (52, 24), 0, 0, 360, (20, 20, 20), 2)     # B circled
    assert marks.decide([marks.mark_score(scan, page, b) for b in boxes]) == [1]


def test_student_ink_drops_the_printed_template():
    page = mc_page()
    scan = page.copy()
    cv2.putText(scan, "42", (700, 370), FONT, 1.0, (40, 40, 40), 2)          # the student's answer
    ink = marks.student_ink(scan, page, SHORT_RECT)
    g = ink[:, :, 0]
    x, y = SHORT_RECT[:2]
    assert (g[335 - 25 - y:335 - y, 570 - x:660 - x] < 128).sum() == 0         # "Answer:" is gone
    assert (g[345 - y:372 - y, 700 - x:750 - x] < 128).sum() > 100              # "42" is kept


def test_answers_are_compared_without_spaces_case_or_punctuation():
    assert autogroup.normalize_answer(" X = 3. ") == autogroup.normalize_answer("x=3") == "x=3"
    assert autogroup.normalize_answer("−2") == "-2"
    assert autogroup.normalize_answer("2") != autogroup.normalize_answer("-2")


# ---------- suggested groups saved for hand grading ----------
@pytest.fixture(scope="module")
def mcexam(world, data_dir):
    d = data_dir / "mc"
    d.mkdir(exist_ok=True)
    page = mc_page()
    p = str(d / "t.png")
    save_image(p, page)
    t = create_template_from_image("MC vA", p, "MC quiz", "A")
    q = add_region(t, "Q1", "g", MC_RECT, 2)
    q.rubric = Rubric(max_points=2, items=[RubricItem(2, "correct")])
    s = add_region(t, "Q2", "g", SHORT_RECT, 1)
    s.rubric = Rubric(max_points=1, items=[RubricItem(1, "correct")])
    t.id = persist(t)
    marked = [(0,), (0,), (2,), (), (0, 2)]
    subs = []
    for k, ks in enumerate(marked):
        sp = str(d / f"s{k}.png")
        img = filled(page, *ks)
        cv2.putText(img, ["42", " 42.", "17", "", "x = 3"][k], (690, 372), FONT, 1.0, (40, 40, 40), 2)
        save_image(sp, img)
        sub = Submission(exam_name="MC quiz", template_id=t.id, status=SUB_READY, scan_pages=[sp],
                         name_confirmed=True)
        db.add_submission(sub)
        subs.append(sub.id)
    choices = [{"label": lab, "rect": b} for lab, b in zip("ABCD", marks.detect_choices(page, MC_RECT))]
    return {"tid": t.id, "subs": subs, "choices": choices, "page": page, "dir": d}


def _clear(t, i):
    for g in hand.groups(t, i):
        db.delete_hand_group(g["id"])
    for s in db.list_submissions(template_id=t.id):
        for g in db.list_grades(s.id):
            if g.region_id == i:
                db.delete_grade(g.id)


def test_multiple_choice_groups_and_late_scans(mcexam):
    t = db.load_template(mcexam["tid"])
    _clear(t, 0)
    s = mcexam["subs"]
    answers = hand.prepare_answers(t, 0)
    summary = autogroup.suggest_groups(t, 0, autogroup.KIND_CHOICE, answers, mcexam["choices"])
    got = {g["label"]: g for g in hand.groups(t, 0)}
    assert {k: g["members"] for k, g in got.items()} == {"Blank": [s[3]], "A": [s[0], s[1]], "C": [s[2]],
                                                         "A + C": [s[4]]}
    assert got["Blank"]["is_blank"] and all(not g["checked"] and g["auto_key"] for g in got.values())
    assert (summary.grouped, summary.groups, summary.unclear) == (5, 4, 0)

    # grading a group counts as checking it
    hand.grade_group(t, 0, got["A"], [t.regions[0].rubric.items[0].id])
    assert next(g for g in hand.groups(t, 0) if g["label"] == "A")["checked"]

    # a late scan joins its group (to be checked again); grouped or hand-graded answers never move
    db.delete_hand_group(got["C"]["id"])
    hand.save_hand_grade(t, 0, s[2], [])                       # graded on its own meanwhile
    late = Submission(exam_name="MC quiz", template_id=t.id, status=SUB_READY, name_confirmed=True,
                      scan_pages=[str(mcexam["dir"] / "late.png")])
    save_image(late.scan_pages[0], filled(mcexam["page"], 0))
    db.add_submission(late)
    try:
        summary = autogroup.suggest_groups(t, 0, autogroup.KIND_CHOICE, hand.prepare_answers(t, 0),
                                           mcexam["choices"])
        after = {g["label"]: g for g in hand.groups(t, 0)}
        assert after["A"]["members"] == [s[0], s[1], late.id] and not after["A"]["checked"]
        assert "C" not in after and summary.skipped == 5 and summary.grouped == 1
    finally:
        db.delete_submission(late.id)
        _clear(t, 0)


class FakeReader:
    """TrOCR stand-in: answers in call order; checks it only gets the student's ink."""
    def __init__(self, readings):
        self.readings, self.images = list(readings), []

    def read(self, img):
        self.images.append(img)
        return self.readings.pop(0)


def test_short_answers_with_the_same_reading_are_grouped(mcexam, monkeypatch):
    from lmgrader.students import trocr
    t = db.load_template(mcexam["tid"])
    _clear(t, 1)
    s = mcexam["subs"]
    # s3 wrote nothing (not read); "17" is the only one of its kind; "x = 3" was read unsure
    reader = FakeReader([("42", 0.9), ("42.", 0.8), ("17", 0.9), ("x = 3", 0.3)])
    monkeypatch.setattr(trocr, "get_reader", lambda: reader)
    try:
        summary = autogroup.suggest_groups(t, 1, autogroup.KIND_SHORT, hand.prepare_answers(t, 1))
        gs = hand.groups(t, 1)
        assert [(g["label"], g["members"], g["checked"]) for g in gs] == [("“42”", [s[0], s[1]], False)]
        assert (summary.grouped, summary.unclear) == (2, 3) and len(reader.images) == 4
        p = CROP_PADDING                                    # the printed "Answer:" is not sent to TrOCR
        assert all((im[p + 10:p + 36, p + 8:p + 102, 0] < 128).sum() == 0 for im in reader.images)
        assert all((im[:, :, 0] < 128).sum() > 50 for im in reader.images)
        monkeypatch.setattr(trocr, "get_reader", lambda: None)
        assert autogroup.suggest_groups(t, 1, autogroup.KIND_SHORT, hand.prepare_answers(t, 1)).no_reader
    finally:
        _clear(t, 1)


# ---------- UI ----------
def _wait(qapp, cond, timeout=30):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert cond()


def test_choice_editor_finds_adds_deletes_and_renames(qapp, mcexam):
    from lmgrader.ui.group_setup import ChoiceEditor
    dlg = ChoiceEditor(None, mcexam["page"], MC_RECT, "Q1")
    assert [c["label"] for c in dlg.choices] == ["A", "B", "C", "D"]
    assert dlg.result_choices()[0]["rect"] == mcexam["choices"][0]["rect"]
    dlg.add_box((10, 10, 40, 30))                     # canvas (crop) coordinates
    assert dlg.choices[-1]["label"] == "E" and dlg.choices[-1]["rect"][:2] == (10 + dlg.ox, 10 + dlg.oy)
    dlg.list.item(0).setText("True")
    dlg.select(4)
    dlg.delete_selected()
    assert [c["label"] for c in dlg.result_choices()] == ["True", "B", "C", "D"]
    dlg.list.item(1).setText("True")                  # duplicate names are made unique
    assert [c["label"] for c in dlg.result_choices()][:2] == ["True", "True2"]
    dlg.close()


def test_ask_kind_dialog(qapp):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QRadioButton
    from lmgrader.ui import group_setup
    seen = {}

    def answer():
        dlg = QApplication.activeModalWidget()
        seen["preselected"] = dlg.findChild(QRadioButton, "kind_choice").isChecked()
        seen["short_enabled"] = dlg.findChild(QRadioButton, "kind_short").isEnabled()
        dlg.findChild(QRadioButton, "kind_blank").setChecked(True)
        dlg.accept()
    QTimer.singleShot(50, answer)
    assert group_setup.ask_kind(None, "Q1", suggested="choice", reader_ok=False) == "blank"
    assert seen == {"preselected": True, "short_enabled": False}


def test_ask_kind_shows_the_question_zoomed_out(qapp):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QLabel
    from conftest import SCREENSHOT_DIR, make_page
    from lmgrader.ui import group_setup
    page = make_page(57, "3. Is every square a rectangle?")       # text at y≈60, box below it
    view = group_setup.question_view(page, (60, 120, 300, 80))
    H, W = page.shape[:2]
    assert view.shape[1] == W and view.shape[0] >= 80 + H // 8      # full width, room above the box
    assert (view[120, 60:360] != page[120, 60:360]).any()            # the box is outlined
    tall = group_setup.question_view(page, (60, 700, 300, 80), question_rect=(60, 300, 500, 60))
    assert tall.shape[0] >= 700 + 80 - 300                         # reaches up to the question area
    seen = {}

    def answer():
        dlg = QApplication.activeModalWidget()
        pic = dlg.findChild(QLabel, "kind_preview")
        seen["pic"] = pic is not None and not pic.pixmap().isNull()
        seen["fits"] = pic is not None and pic.pixmap().width() <= group_setup.PREVIEW_W
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        dlg.grab().save(str(SCREENSHOT_DIR / "ask_kind_preview.png"))
        dlg.accept()
    QTimer.singleShot(50, answer)
    assert group_setup.ask_kind(None, "Q3", page_img=page, rect=(60, 120, 300, 80)) == "blank"
    assert seen == {"pic": True, "fits": True}


def test_hand_grade_view_multiple_choice(qapp, mcexam, monkeypatch):
    from lmgrader.ui import group_setup
    from lmgrader.ui.hand_grade_view import HandGradeView, PAGE_GROUP, PAGE_GRADE
    t = db.load_template(mcexam["tid"])
    _clear(t, 0)
    uid = t.regions[0].uid
    db.set_hand_stage(t.id, uid, "start")
    asked = {}
    monkeypatch.setattr(group_setup, "ask_kind",
                        lambda parent, problem, current, suggested, reader_ok, page_img=None, rect=None,
                        question_rect=None: (asked.update(s=suggested, page=page_img is not None, rect=rect),
                                             "choice")[1])
    monkeypatch.setattr(group_setup, "edit_choices", lambda parent, img, rect, problem, choices: mcexam["choices"])
    v = HandGradeView()
    v.resize(1300, 850); v.show()
    try:
        v.cb_exam.setCurrentIndex(v.cb_exam.findText("MC quiz"))
        v.cb_question.setCurrentIndex(v.cb_question.findData(uid))
        v.open_question()
        _wait(qapp, lambda: v.worker is None and v.stack.currentIndex() == PAGE_GROUP)
        assert asked["s"] == "choice"                                  # the page has bubbles
        assert asked["page"] and asked["rect"] == t.regions[0].rect     # the question is shown
        assert db.get_hand_setup(t.id, uid) == ("choice", [{"label": c["label"], "rect": tuple(c["rect"])}
                                                           for c in mcexam["choices"]])
        texts = [v.list_groups.item(r).text() for r in range(v.list_groups.count())]
        assert texts[0].startswith("⚑ Blank") and any(x.startswith("⚑ A + C") for x in texts)
        assert "suggested 4 groups (5 answers)" in v.lbl_progress.text()
        assert v.btn_check_group.isVisible() and not v.btn_check_group.isEnabled()
        v.list_groups.setCurrentRow(1)
        assert v.btn_check_group.isEnabled()
        v.check_group()
        assert not v.list_groups.item(1).text().startswith("⚑") and v.list_groups.currentRow() == 2
        v._set_stage("grade")
        assert v.stack.currentIndex() == PAGE_GRADE
        assert "⚑ Blank" in v.list_queue.item(0).text() and "Suggested group" in v.lbl_applies.text()
        v.save_current()                                               # grading the Blank group checks it
        assert db.list_hand_groups(t.id, uid)[0]["checked"]
    finally:
        v.close()
        _clear(t, 0)
