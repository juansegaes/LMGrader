"""Editing existing boxes on the template canvas: select, resize by handle, move, nudge."""
import pytest
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from lmgrader import db
from lmgrader.template.alignment import save_image
from conftest import make_page, SCREENSHOT_DIR


@pytest.fixture
def tv(qapp, tmp_path):
    from lmgrader.ui.main_window import MainWindow
    p = str(tmp_path / "box_page.png")
    save_image(p, make_page(81, "BOXES"))
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    v = mw.template_view
    v.create_exam("Box editing", [p])
    v.on_rect_drawn((100, 300, 300, 150))
    v.on_rect_drawn((100, 600, 300, 150))
    v.region_list.setCurrentRow(0)
    qapp.processEvents()
    yield v
    v.flush(); mw.close()


def _pt(canvas, x, y):
    s = canvas.scale()
    return QPoint(int(x * s), int(y * s))


def _drag(canvas, a, b, mod=Qt.KeyboardModifier.NoModifier):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, mod, a)
    for k in range(1, 6):   # a few intermediate moves, like a real drag
        QTest.mouseMove(canvas, a + (b - a) * k / 5)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, mod, b)


def test_resize_move_nudge(tv, qapp):
    c = tv.viewer.canvas
    assert c.active_key == ("a", 0)
    s = c.scale()
    # resize from the bottom-right handle
    _drag(c, _pt(c, 400, 450), _pt(c, 500, 500))
    x, y, w, h = tv.t.regions[0].rect
    assert (x, y) == (100, 300) and abs(w - 400) <= 2 / s + 1 and abs(h - 200) <= 2 / s + 1
    # move by dragging inside
    _drag(c, _pt(c, 200, 350), _pt(c, 250, 400))
    x2, y2, w2, h2 = tv.t.regions[0].rect
    assert abs(x2 - 150) <= 2 / s + 1 and abs(y2 - 350) <= 2 / s + 1 and (w2, h2) == (w, h)
    # arrow keys nudge (Shift = 10 px)
    c.setFocus()
    QTest.keyClick(c, Qt.Key.Key_Right)
    QTest.keyClick(c, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert tv.t.regions[0].rect[:2] == (x2 + 1, y2 + 10)
    tv.flush()
    assert db.load_template(tv.t.id).regions[0].rect == tv.t.regions[0].rect
    assert len(tv.t.regions) == 2                 # editing never adds boxes
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    tv.window().grab().save(str(SCREENSHOT_DIR / "template_editor_handles.png"))


def test_click_selects_then_edits_other_boxes(tv, qapp):
    c = tv.viewer.canvas
    # a drag on a box that is not active draws a new box (old behaviour); a click selects it
    QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=_pt(c, 200, 650))
    assert tv.sel == 1 and c.active_key == ("a", 1)
    # name field: draw, select, resize
    from lmgrader.ui import template_editor as te
    tv.mode_group.button(te.MODE_NAME).click()
    tv.on_rect_drawn((500, 100, 300, 60))
    tv.mode_group.button(te.MODE_ANSWER).click()
    QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=_pt(c, 650, 130))
    assert c.active_key == ("n", None)
    _drag(c, _pt(c, 800, 130), _pt(c, 700, 130))           # right edge handle
    assert tv.t.name_rect[0] == 500 and abs(tv.t.name_rect[2] - 200) <= 3
    # Shift+drag over the active box draws a new region instead of moving it
    QTest.mouseClick(c, Qt.MouseButton.LeftButton, pos=_pt(c, 200, 650))
    n = len(tv.t.regions)
    _drag(c, _pt(c, 150, 620), _pt(c, 300, 700), Qt.KeyboardModifier.ShiftModifier)
    assert len(tv.t.regions) == n + 1 and tv.t.regions[1].rect == (100, 600, 300, 150)


def _answer_keys(canvas):
    return {o.key for o in canvas.overlays if o.key and o.key[0] == "a"}


def test_redraw_box_replaces_only_the_answer_box(tv, qapp):
    from lmgrader.models import Rubric, RubricItem
    c = tv.viewer.canvas
    r = tv.t.regions[0]
    r.points, r.question_text = 4, "x^2 + 1"
    r.rubric = Rubric(max_points=4, items=[RubricItem(4, "right answer")])
    uid, item_id = r.uid, r.rubric.items[0].id
    tv.btn_redraw_box.click()
    assert tv.btn_redraw_box.isChecked() and ("a", 0) not in _answer_keys(c) and c.active_key is None
    assert "new answer box of Q1" in tv.lbl_hint.text()
    # a real drag right over where the old box was draws the new box (it doesn't move the old one)
    _drag(c, _pt(c, 120, 320), _pt(c, 520, 420))
    r = tv.t.regions[0]
    assert len(tv.t.regions) == 2 and r.uid == uid
    x, y, w, h = r.rect
    assert abs(x - 120) <= 3 and abs(y - 320) <= 3 and abs(w - 400) <= 3 and abs(h - 100) <= 3
    assert (r.points, r.question_text, r.rubric.items[0].id) == (4, "x^2 + 1", item_id)
    assert not tv.btn_redraw_box.isChecked() and ("a", 0) in _answer_keys(c) and c.active_key == ("a", 0)
    tv.flush()
    saved = db.load_template(tv.t.id).regions[0]
    assert saved.rect == r.rect and saved.uid == uid and saved.rubric.items[0].id == item_id
    # the next drag adds a new question again
    _drag(c, _pt(c, 600, 700), _pt(c, 800, 800))
    assert len(tv.t.regions) == 3


def test_redraw_box_is_cancelled_by_esc_another_question_or_mode(tv, qapp):
    from lmgrader.ui import template_editor as te
    c = tv.viewer.canvas
    before = tv.t.regions[0].rect
    tv.btn_redraw_box.click()
    tv.window().activateWindow()                  # shortcuts only reach the active window
    QTest.qWaitForWindowActive(tv.window())
    c.setFocus()
    QTest.keyClick(c, Qt.Key.Key_Escape)
    assert not tv.btn_redraw_box.isChecked() and ("a", 0) in _answer_keys(c)
    tv.btn_redraw_box.click()
    tv.btn_redraw_box.click()                     # clicking it again keeps the old box too
    assert tv._redraw_uid is None and ("a", 0) in _answer_keys(c)
    tv.btn_redraw_box.click()
    tv.region_list.setCurrentRow(1)
    assert tv._redraw_uid is None and ("a", 0) in _answer_keys(c)
    tv.btn_redraw_box.click()                     # Q2 now
    tv.mode_group.button(te.MODE_NAME).click()
    assert tv._redraw_uid is None
    tv.on_rect_drawn((500, 100, 300, 60))         # a name field, not Q2's box
    assert tv.t.regions[0].rect == before and tv.t.regions[1].rect == (100, 600, 300, 150)
    assert tv.t.name_rect == (500, 100, 300, 60) and len(tv.t.regions) == 2


def test_redraw_box_can_move_a_question_to_another_page(qapp, tmp_path):
    from lmgrader.ui.main_window import MainWindow
    pages = []
    for k in (1, 2):
        pages.append(str(tmp_path / f"p{k}.png"))
        save_image(pages[-1], make_page(90 + k, f"PAGE {k}"))
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        tv.create_exam("Two pages", pages)
        tv.on_rect_drawn((100, 300, 300, 150))
        tv.t.regions[0].question_rect = (100, 200, 300, 80)
        tv.region_list.setCurrentRow(0)
        tv.btn_redraw_box.click()
        tv.set_page(1)                            # the right place is on page 2
        assert tv._redraw_uid is not None
        tv.on_rect_drawn((150, 400, 300, 150))
        r = tv.t.regions[0]
        assert (r.page, r.rect, r.question_rect) == (1, (150, 400, 300, 150), None)
        assert len(tv.t.regions) == 1 and "p2" in tv.region_list.item(0).text()
    finally:
        tv.flush(); mw.close()


def test_without_llm_there_is_no_question_area(tv, qapp, monkeypatch):
    from lmgrader.config import LLM_CONFIG
    from lmgrader.ui import template_editor as te
    c = tv.viewer.canvas
    tv.t.regions[0].question_rect = (100, 200, 300, 80)
    tv.mode_group.button(te.MODE_QUESTION).click()
    tv._refresh_overlays()
    assert ("q", 0) in {o.key for o in c.overlays}
    monkeypatch.setitem(LLM_CONFIG, "enabled", False)
    tv.apply_llm_mode()
    assert tv.mode_group.button(te.MODE_QUESTION).isHidden()
    assert tv.mode_group.checkedId() == te.MODE_ANSWER        # never left in a hidden mode
    assert ("q", 0) not in {o.key for o in c.overlays} and ("a", 0) in _answer_keys(c)
    assert tv.t.regions[0].question_rect == (100, 200, 300, 80)   # kept for when the LLM is back
    monkeypatch.setitem(LLM_CONFIG, "enabled", True)
    tv.apply_llm_mode()
    assert not tv.mode_group.button(te.MODE_QUESTION).isHidden()
    assert ("q", 0) in {o.key for o in c.overlays}


def test_no_editing_while_llm_runs(tv, qapp):
    c = tv.viewer.canvas
    before = tv.t.regions[0].rect
    c.draw_enabled = False         # what _update_enabled does while a worker runs
    _drag(c, _pt(c, 200, 350), _pt(c, 260, 420))
    assert tv.t.regions[0].rect == before and len(tv.t.regions) == 2
    c.draw_enabled = True
