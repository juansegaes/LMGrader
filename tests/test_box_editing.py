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


def test_no_editing_while_llm_runs(tv, qapp):
    c = tv.viewer.canvas
    before = tv.t.regions[0].rect
    c.draw_enabled = False         # what _update_enabled does while a worker runs
    _drag(c, _pt(c, 200, 350), _pt(c, 260, 420))
    assert tv.t.regions[0].rect == before and len(tv.t.regions) == 2
    c.draw_enabled = True
