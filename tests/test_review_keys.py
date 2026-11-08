"""Review: question chips and keyboard navigation (Alt+arrows, Ctrl+Enter, F3)."""
import pytest
from PyQt6.QtGui import QShortcut
from lmgrader import db
from lmgrader.grading import session, Grader
from lmgrader.models import Student
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

EXAM = "KeysExam"


class FlaggingLLM(FakeLLM):
    def grade_answer(self, *a, **kw):
        res = super().grade_answer(*a, **kw)
        res.update(needs_human_review=True, review_reason="illegible")
        return res


@pytest.fixture
def keys_world(tmp_path):
    """One-page exam with Q1-Q3; tests A and C are flagged on every question, B on none."""
    p = str(tmp_path / "t.png"); save_image(p, make_page(51, "KEYS"))
    t = create_template_from_image(f"{EXAM} vA", [p], EXAM, "A")
    for i, lab in enumerate(("Q1", "Q2", "Q3")):
        add_region(t, lab, "g", (100, 200 + 250 * i, 300, 150), 4,
                   rubric="- [4 pt] both roots").question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    scans = []
    for k in range(3):
        sp = str(tmp_path / f"s{k}.png"); save_image(sp, make_page(51, "KEYS")); scans.append(sp)
    subs, _ = session.create_submissions(EXAM, scans, 1, source="keys.pdf")
    out = {}
    for sub, name, llm in zip(subs, ("Keys A", "Keys B", "Keys C"), (FlaggingLLM(), FakeLLM(), FlaggingLLM())):
        sub.template_id = t.id
        session.confirm_student(sub, db.add_student(Student(name=name)))
        session.grade_one(Grader(llm), sub)
        out[name] = sub
    yield out
    for sub in subs:
        db.delete_submission(sub.id)


@pytest.fixture
def view(qapp, keys_world):
    from lmgrader.ui.review_view import ReviewView
    v = ReviewView(); v.resize(1300, 850); v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findData(EXAM))
    qapp.processEvents()
    yield v
    v.close()


def _open(v, qapp, sub):
    row = next(i for i, (s, _) in enumerate(v._rows) if s.id == sub.id)
    v.table.selectRow(row)
    for _ in range(3):
        qapp.processEvents()
    return row


def test_chips_show_state_and_jump(qapp, keys_world, view):
    v = view
    _open(v, qapp, keys_world["Keys A"])
    assert [b.text() for b in v.chips] == ["⚠ Q1  3/4", "⚠ Q2  3/4", "⚠ Q3  3/4"]
    # a new test starts on its first question (outlined, chip checked)
    assert v._cur_card is v.grade_cards[0] and v.chips[0].isChecked()
    assert v.grade_cards[0].property("current")
    # a chip jumps to its question and puts the cursor in its score box
    v.chips[2].click(); qapp.processEvents()
    c2 = v.grade_cards[2]
    assert v._cur_card is c2 and [b.isChecked() for b in v.chips] == [False, False, True]
    assert c2.property("current") and not v.grade_cards[0].property("current")
    sb = v.detail_scroll.verticalScrollBar()
    assert sb.value() == min(c2.y(), sb.maximum()) and sb.value() > 0
    # the chips are not in the scrolling part: still on screen after the jump
    assert v.chip_bar.isVisible() and not v.detail.isAncestorOf(v.chip_bar)
    # a score typed by the professor turns the chip into a human grade
    v.grade_cards[1].spin.setValue(2.5); qapp.processEvents()
    assert v.chips[1].text() == "✓ Q2  2.5/4"
    # the clean test: AI grades, no mark
    _open(v, qapp, keys_world["Keys B"])
    assert [b.text() for b in v.chips] == ["Q1  3/4", "Q2  3/4", "Q3  3/4"]


def test_question_and_test_keys(qapp, keys_world, view):
    v = view
    row = _open(v, qapp, keys_world["Keys A"])
    cards = v.grade_cards
    assert v.next_question() and v._cur_card is cards[1]
    assert v.next_question() and v._cur_card is cards[2]
    assert not v.next_question() and v._cur_card is cards[2]      # last question: stays
    assert v.prev_question() and v._cur_card is cards[1]
    # clicking/tabbing into a question makes it the current one
    assert v._card_of(cards[0].fb_edit) is cards[0]
    v._focus_moved(None, cards[0].fb_edit)
    assert v._cur_card is cards[0] and v.chips[0].isChecked() and not v.chips[1].isChecked()
    # tests: next / previous row of the list, opened at its first question
    if row + 1 < v.table.rowCount():
        assert v.next_test() == row + 1 and v._selected_row() == row + 1
        assert v._cur_card is v.grade_cards[0]
        assert v.prev_test() == row
    last = v.table.rowCount() - 1
    v.table.selectRow(last); qapp.processEvents()
    assert v.next_test() == -1 and v._selected_row() == last          # last test: stays


def test_accept_and_next(qapp, keys_world, view):
    v = view
    a, c = keys_world["Keys A"], keys_world["Keys C"]
    _open(v, qapp, a)
    first = v.grade_cards[0]
    # Ctrl+Enter accepts the suggestion of the current question and moves to the next flagged one
    assert v.accept_and_next() == v._selected_row()
    g = db.list_grades(a.id)[0]
    assert g.human_graded and not g.needs_human_review and g.score == 3
    assert first.g.human_graded and v._cur_card is v.grade_cards[1]
    assert v.chips[0].text().startswith("✓ ")
    # accepting hid Q1's banner (Q1 got shorter): Q2's top must still be at the top of the view
    for _ in range(3):
        qapp.processEvents()
    sb = v.detail_scroll.verticalScrollBar()
    assert abs(sb.value() - min(v.grade_cards[1].y(), sb.maximum())) <= 1
    # a typed score counts as the grade (not the AI suggestion)
    v.grade_cards[1].spin.setValue(1.0)
    v.accept_and_next()
    assert db.list_grades(a.id)[1].score == 1.0 and v._cur_card is v.grade_cards[2]
    # the last flagged question of A: on to the next test that needs review (C)
    v.accept_and_next()
    for _ in range(3):
        qapp.processEvents()
    assert not any(x.needs_human_review for x in db.list_grades(a.id))
    assert v._current.id == c.id and v._cur_card is v.grade_cards[0]
    assert v._cur_card.g.needs_human_review


def test_shortcuts_registered(qapp, view):
    from lmgrader.ui import review_view as rv
    keys = {sc.key().toString() for sc in view.findChildren(QShortcut)}
    for group in (rv.KEYS_NEXT_QUESTION, rv.KEYS_PREV_QUESTION, rv.KEYS_NEXT_TEST, rv.KEYS_PREV_TEST,
                  rv.KEYS_ACCEPT_NEXT, rv.KEYS_NEXT_FLAGGED):
        for k in group:
            assert k in keys, k


def test_real_key_presses(qapp, keys_world, view):
    """The keys reach the page from the test list, the score box and the feedback box."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    v = view
    _open(v, qapp, keys_world["Keys A"])
    v.activateWindow(); v.table.setFocus(); qapp.processEvents()
    if QApplication.activeWindow() is not v:
        pytest.skip("this platform cannot activate the window")
    alt, ctrl = Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.ControlModifier
    QTest.keyClick(v.table, Qt.Key.Key_Down, alt); qapp.processEvents()
    assert v._cur_card is v.grade_cards[1] and QApplication.focusWidget() is v.grade_cards[1].spin
    fb = v.grade_cards[1].fb_edit
    fb.setFocus(); fb.setPlainText("Check the sign."); qapp.processEvents()
    QTest.keyClick(fb, Qt.Key.Key_Return, ctrl); qapp.processEvents()
    g = db.list_grades(keys_world["Keys A"].id)[1]
    assert g.human_graded and g.feedback == "Check the sign."          # saved, no line break added
    assert v._cur_card is v.grade_cards[2]
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Up, alt); qapp.processEvents()
    assert v._cur_card is v.grade_cards[1]
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_F3); qapp.processEvents()
    assert v._cur_card is v.grade_cards[2]


def test_flow_layout_wraps(qapp):
    """Many chips wrap onto more lines instead of widening the page."""
    from PyQt6.QtWidgets import QPushButton, QWidget
    from lmgrader.ui.widgets import FlowLayout
    w = QWidget()
    flow = FlowLayout(w, spacing=6)
    buttons = [QPushButton(f"⚠ Q{i}a  3/4") for i in range(20)]
    for b in buttons:
        flow.addWidget(b)
    w.resize(300, flow.heightForWidth(300)); w.show(); qapp.processEvents()
    one_row = buttons[0].sizeHint().height()
    assert flow.heightForWidth(300) > 3 * one_row
    assert all(b.geometry().right() <= 300 for b in buttons)
    assert len({b.geometry().y() for b in buttons}) > 3
    buttons[0].hide(); qapp.processEvents()          # hidden widgets take no room
    flow.setGeometry(w.rect())
    assert buttons[1].geometry().x() == 0
    w.close()


def test_screenshot_chips(qapp, keys_world, view):
    v = view
    _open(v, qapp, keys_world["Keys A"])
    v.grade_cards[0].spin.setValue(4.0)
    v.next_question(); qapp.processEvents()
    for _ in range(3):
        qapp.processEvents()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    v.grab().save(str(SCREENSHOT_DIR / "review_chips.png"))
