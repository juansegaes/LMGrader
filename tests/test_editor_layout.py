"""Template editor v3: Stop buttons, two-phase auto-setup, blank (back) pages, columns, ID field."""
import threading
import time
from pathlib import Path
import pytest
from lmgrader import db
from lmgrader.models import BLANK
from lmgrader.template.alignment import save_image
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "test2a.pdf"


class LLM(FakeLLM):
    """detect_layout with the new `columns` hint; extract_question / detect can be made to block
    until Stop aborts them (like a slow local model)."""
    def __init__(self):
        super().__init__()
        self.layout_calls, self.extracted, self.columns_seen = 0, 0, []
        self.block_extract_after = None     # block on the (n+1)-th extraction
        self.block_detect_after = None
        self.aborted = threading.Event()
        self.layouts = None                 # optional per-page layouts

    def abort(self):
        self.aborted.set()

    def _block(self):
        if not self.aborted.wait(10):
            raise AssertionError("never aborted")
        raise RuntimeError("connection closed")       # what an aborted HTTP request looks like

    def detect_layout(self, image_b64, width, height, sent_w=None, sent_h=None, columns=0):
        self.layout_calls += 1
        self.columns_seen.append(columns)
        if self.block_detect_after is not None and self.layout_calls > self.block_detect_after:
            self._block()
        page = self.layout_calls
        if self.layouts:
            return self.layouts[page - 1]
        return {"questions": [{"problem": str(page), "question_rect": (100, 200, 400, 60),
                               "answer_rect": (100, 300, 300, 150), "points": 2}],
                "name_rect": (500, 100, 300, 60) if page == 1 else None,
                "id_rect": (500, 180, 300, 60) if page == 1 else None}

    def extract_question(self, b):
        if self.block_extract_after is not None and self.extracted >= self.block_extract_after:
            self._block()
        self.extracted += 1
        return rf"Q{self.extracted}: Solve $x^2-4=0$."


@pytest.fixture
def pages(tmp_path):
    out = []
    for i in range(2):
        p = str(tmp_path / f"blank_p{i + 1}.png")
        save_image(p, make_page(200 + i, f"PAGE {i + 1}"))
        out.append(p)
    return out


@pytest.fixture
def editor(qapp, monkeypatch):
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    llm = LLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    monkeypatch.setattr(te, "refine_layout", lambda img, layout: layout)
    errors = []
    monkeypatch.setattr(te.QMessageBox, "critical", staticmethod(lambda *a, **k: errors.append(a)))
    mw = MainWindow()
    mw.resize(1400, 880)
    mw.show()
    qapp.processEvents()
    yield mw.template_view, llm, qapp, mw, errors
    mw.template_view.flush()
    mw.close()


def wait_for(qapp, cond, timeout=10.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert cond(), "timed out"


def test_stop_extraction_keeps_finished_items(editor, pages):
    tv, llm, qapp, _, errors = editor
    t = tv.create_exam("Stop exam", pages)
    for y in (300, 600):
        tv.on_rect_drawn((100, y, 300, 150))
    llm.block_extract_after = 1                       # 1st question returns, 2nd hangs
    tv.run_ai("extract")
    assert tv.btn_stop.isVisibleTo(tv) and tv.btn_stop.isEnabled()
    wait_for(qapp, lambda: tv.t.regions[0].question_text != "")
    tv.stop_ai()
    wait_for(qapp, lambda: tv.worker is None)
    assert llm.aborted.is_set()                        # in-flight request was aborted
    assert tv.t.regions[0].question_text and not tv.t.regions[1].question_text
    assert tv.ai_status.text().startswith("Stopped") and errors == []     # no error dialog
    assert not tv.btn_stop.isVisibleTo(tv) and tv.btn_autosetup.isEnabled()
    assert db.load_template(t.id).regions[0].question_text          # kept + saved


def test_stop_detection_keeps_processed_pages(editor, pages):
    tv, llm, qapp, _, errors = editor
    tv.create_exam("Stop detect", pages)
    tv.set_page(1)
    tv.on_rect_drawn((50, 50, 200, 100))              # an existing region on page 2
    llm.block_detect_after = 1                        # page 1 detected, page 2 hangs
    tv.start_detect(autosetup=True, replace=True)
    wait_for(qapp, lambda: llm.layout_calls == 2)
    tv.stop_ai()
    wait_for(qapp, lambda: tv.worker is None)
    regs = tv.t.regions
    # page 1 replaced by the detection, page 2's region kept (never reached)
    assert [(r.page, r.problem) for r in regs] == [(0, "Q1"), (1, "Q1")]
    assert regs[1].rect == (50, 50, 200, 100)
    assert not tv.latex_pending() and errors == [] and tv.ai_status.text().startswith("Stopped")


def test_two_phase_autosetup_waits_for_confirmation(editor, pages):
    tv, llm, qapp, _, _ = editor
    tv.create_exam("Two phase", pages)
    tv.start_detect(autosetup=True, replace=True)
    wait_for(qapp, lambda: tv.worker is None)
    assert len(tv.t.regions) == 2 and llm.extracted == 0
    assert tv.latex_pending() and tv.confirm_banner.isVisibleTo(tv)
    assert tv.t.id_rect == (500, 180, 300, 60) and tv.t.name_rect == (500, 100, 300, 60)
    assert [r.points for r in tv.t.regions] == [1.0, 1.0]   # printed "2" ignored: default is always 1
    tv.set_page(1)                                    # banner survives page switches
    assert tv.confirm_banner.isVisibleTo(tv)
    tv.region_list.setCurrentRow(0)
    tv.in_points.setValue(5)                          # the user adjusts points while checking
    tv.continue_latex()
    assert not tv.confirm_banner.isVisibleTo(tv)
    wait_for(qapp, lambda: tv.worker is None)
    assert llm.extracted == 2 and all(r.question_text for r in tv.t.regions)
    assert tv.t.regions[0].points == 5
    # "Not now" dismisses without extracting
    tv.start_detect(autosetup=True, replace=True)
    wait_for(qapp, lambda: tv.worker is None)
    assert tv.latex_pending()
    tv.btn_not_now.click()
    assert not tv.latex_pending() and llm.extracted == 2


def test_double_sided_and_blank_pages(editor, pages):
    tv, _, qapp, _, _ = editor
    t = tv.create_exam("Double sided", pages)
    assert tv.t.scan_page_count == 2 and not tv.chk_double.isChecked()
    tv.chk_double.setChecked(True)
    assert tv.t.scan_layout == [0, BLANK, 1, BLANK]
    assert "4 scanned pages" in tv.lbl_layout.text() and "1 · B · 2 · B" in tv.lbl_layout.text()
    tv.flush()
    saved = db.load_template(t.id)
    assert saved.scan_page_count == 4 and saved.blank_count == 2
    tv.chk_double.setChecked(False)
    assert tv.t.scan_layout == [] and tv.t.scan_page_count == 2
    # custom layout: a scratch page after page 1 only
    tv.insert_blank(0)
    assert tv.t.layout == [0, BLANK, 1] and not tv.chk_double.isChecked()
    tv.remove_blank(0)                                # not a blank -> no-op
    assert tv.t.layout == [0, BLANK, 1]
    tv.remove_blank(1)
    assert tv.t.layout == [0, 1]
    # the dialog edits the same layout; blank pages never appear in the page selector
    from lmgrader.ui.template_editor import ScanLayoutDialog
    dlg = ScanLayoutDialog(tv, tv)
    dlg.list.setCurrentRow(1)
    dlg._insert()
    assert tv.t.layout == [0, 1, BLANK] and dlg.list.count() == 3
    dlg._remove()
    assert tv.t.layout == [0, 1]
    assert len(tv.page_group.buttons()) == 2
    dlg.close()
    # loading the version restores the controls
    tv.chk_double.setChecked(True)
    tv.flush()
    tv.load(t.id)
    assert tv.chk_double.isChecked()


def test_columns_persist_and_reach_detection(editor, pages):
    tv, llm, qapp, _, _ = editor
    t = tv.create_exam("Columns", pages)
    tv.cb_columns.setCurrentIndex(tv.cb_columns.findData(2))
    assert tv.t.columns == 2
    tv.flush()
    assert db.load_template(t.id).columns == 2
    tv.start_detect(autosetup=False, replace=True)
    wait_for(qapp, lambda: tv.worker is None)
    assert llm.columns_seen == [2, 2]
    tv.load(t.id)
    assert tv.cb_columns.currentData() == 2


def test_id_field_drawing_persists(editor, pages):
    from lmgrader.ui import template_editor as te
    tv, _, qapp, _, _ = editor
    t = tv.create_exam("ID field", pages)
    tv.mode_group.button(te.MODE_ID).setChecked(True)
    tv.on_mode_changed(te.MODE_ID)
    tv.set_page(1)
    tv.on_rect_drawn((600, 120, 300, 60))
    tv.mode_group.button(te.MODE_ANSWER).setChecked(True)
    assert tv.t.id_rect == (600, 120, 300, 60) and tv.t.id_page == 1
    assert any(o.key == ("i", None) for o in tv.viewer.canvas.overlays)
    assert "ID ✓ p2" in tv.field_status() and "No name" in tv.field_status()
    tv.flush()
    saved = db.load_template(t.id)
    assert saved.id_rect == (600, 120, 300, 60) and saved.id_page == 1


@pytest.mark.skipif(not FIXTURE_PDF.exists(), reason="sample exam missing")
def test_screenshot_real_template(editor):
    """Realistic screenshot: the sample 4-page exam, double-sided, 2 columns, phase 1 done."""
    from lmgrader.scanner.image_input import import_pages
    tv, llm, qapp, mw, _ = editor
    pages = import_pages(str(FIXTURE_PDF), "templates")
    assert len(pages) == 4
    # boxes roughly where test2a page 1 has them (300 dpi: 2550 x 3300)
    p1 = {"questions": [
        {"problem": "1a", "question_rect": (210, 1280, 1050, 70), "answer_rect": (282, 1460, 975, 770), "points": None},
        {"problem": "1b", "question_rect": (210, 2240, 1050, 70), "answer_rect": (282, 2350, 975, 830), "points": None},
        {"problem": "2a", "question_rect": (1370, 1380, 1100, 140), "answer_rect": (1460, 1545, 975, 830), "points": None},
        {"problem": "2b", "question_rect": (1370, 2420, 1100, 260), "answer_rect": (1460, 2720, 975, 420), "points": None}],
        "name_rect": (115, 760, 1100, 120), "id_rect": (1335, 760, 1100, 120)}
    empty = {"questions": [], "name_rect": None, "id_rect": None}
    llm.layouts = [p1, empty, empty, empty]
    tv.create_exam("Math 2164 Test 2", pages)
    tv.chk_double.setChecked(True)
    tv.cb_columns.setCurrentIndex(tv.cb_columns.findData(2))
    tv.start_detect(autosetup=True, replace=True)
    wait_for(qapp, lambda: tv.worker is None, timeout=20)
    assert tv.latex_pending() and len(tv.t.regions) == 4
    tv.region_list.setCurrentRow(2)
    qapp.processEvents()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    mw.grab().save(str(SCREENSHOT_DIR / "template_editor_v3.png"))
