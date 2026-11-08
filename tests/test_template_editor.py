"""Template editor: multi-page templates, page switching, auto-setup on all pages, autosave."""
import time
import pytest
from lmgrader import db
from lmgrader.template.alignment import save_image
from conftest import FakeLLM, make_page, SCREENSHOT_DIR


class LayoutLLM(FakeLLM):
    """Fake LLM that also 'detects' one question (+ name on page 1) per page."""
    def __init__(self):
        super().__init__()
        self.layout_calls = 0
        self.extracted = 0

    def detect_layout(self, image_b64, width, height, sent_w=None, sent_h=None):
        self.layout_calls += 1
        page = self.layout_calls
        d = {"questions": [{"problem": str(page), "question_rect": (100, 200, 400, 60),
                            "answer_rect": (100, 300, 300, 150), "points": None}],
             "name_rect": (500, 100, 300, 60) if page == 1 else None}
        return d

    def extract_question(self, b):
        self.extracted += 1
        return rf"Q{self.extracted}: Solve $x^2-4=0$."


@pytest.fixture
def pages(tmp_path):
    out = []
    for i in range(2):
        p = str(tmp_path / f"blank_p{i + 1}.png")
        save_image(p, make_page(100 + i, f"PAGE {i + 1}"))
        out.append(p)
    return out


@pytest.fixture
def editor(qapp, monkeypatch):
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    llm = LayoutLLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    # refine_layout needs real page content; identity keeps the fake boxes predictable
    monkeypatch.setattr(te, "refine_layout", lambda img, layout: layout)
    mw = MainWindow()
    mw.resize(1400, 880)
    mw.show()
    qapp.processEvents()
    yield mw.template_view, llm, qapp, mw
    mw.template_view.flush()
    mw.close()


def wait_for(qapp, cond, timeout=10.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert cond(), "timed out"


def test_multipage_creation_and_page_switching(editor, pages):
    tv, _, qapp, _ = editor
    t = tv.create_exam("Two pager", pages)
    assert t.page_count == 2 and db.load_template(t.id).pages == pages
    assert tv.page == 0 and tv.page_bar_w.isVisibleTo(tv) and len(tv.page_group.buttons()) == 2
    # region drawn on page 2 gets page index 1
    tv.set_page(1)
    tv.on_rect_drawn((100, 300, 300, 150))
    assert tv.t.regions[0].page == 1
    assert "p2" in tv.region_list.item(0).text()
    # name field on page 2
    from lmgrader.ui import template_editor as te
    tv.mode_group.button(te.MODE_NAME).setChecked(True)
    tv.on_rect_drawn((500, 100, 300, 60))
    assert tv.t.name_page == 1
    tv.mode_group.button(te.MODE_ANSWER).setChecked(True)
    # page 1 shows no overlays of page 2; selecting the region switches back to page 2
    tv.set_page(0)
    assert tv.viewer.canvas.overlays == []
    tv.region_list.setCurrentRow(-1)
    tv.region_list.setCurrentRow(0)
    assert tv.page == 1 and any(o.key == ("a", 0) for o in tv.viewer.canvas.overlays)


def test_autosave_persists(editor, pages):
    tv, _, qapp, _ = editor
    t = tv.create_exam("Autosave exam", pages)
    tv.on_rect_drawn((100, 300, 300, 150))
    tv.region_list.setCurrentRow(0)
    tv.in_points.setValue(7)
    assert tv.dirty and tv._autosave.isActive()
    wait_for(qapp, lambda: not tv.dirty, timeout=5)      # debounced timer fired
    saved = db.load_template(t.id)
    assert len(saved.regions) == 1 and saved.regions[0].points == 7
    assert tv.lbl_saved.text() == "Saved ✓"
    # _confirm_discard (used by main_window.closeEvent) flushes immediately
    tv.rubric_editor.add_item(7, "all correct")
    assert tv._confirm_discard() is True and not tv.dirty
    assert [(i.points, i.description) for i in db.load_template(t.id).regions[0].rubric.items] == [(7, "all correct")]
    # the next drawn box starts at 1 again (not the previous question's 7)
    tv.on_rect_drawn((100, 600, 300, 150))
    assert tv.t.regions[1].points == 1.0 and tv.in_points.value() == 1.0


def test_autosetup_two_pages(editor, pages):
    tv, llm, qapp, mw = editor
    t = tv.create_exam("Auto exam", pages)
    assert tv.current_step() == 0
    tv.start_detect(autosetup=True, replace=True)
    assert tv.worker is not None and not tv.btn_autosetup.isEnabled()
    wait_for(qapp, lambda: tv.worker is None, timeout=15)
    assert llm.layout_calls == 2                              # one call per page
    regs = tv.t.regions
    assert [r.problem for r in regs] == ["Q1", "Q2"]           # numbering continues across pages
    assert [r.page for r in regs] == [0, 1]
    # phase 1 stops for confirmation: no LaTeX until the user continues
    assert llm.extracted == 0 and tv.latex_pending() and tv.confirm_banner.isVisibleTo(tv)
    tv.continue_latex()
    wait_for(qapp, lambda: tv.worker is None, timeout=15)
    regs = tv.t.regions
    assert all(r.question_text.startswith("Q") for r in regs)  # LaTeX extracted for every region
    assert regs[0].points == 1.0                               # unprinted points -> default
    assert tv.t.name_rect == (500, 100, 300, 60) and tv.t.name_page == 0
    assert tv.current_step() == 1                              # next: check boxes, set points
    saved = db.load_template(t.id)                             # flushed on finish
    assert [r.page for r in saved.regions] == [0, 1] and all(r.question_text for r in saved.regions)

    # step 3: rubrics crop/use each region's own page; then step 4
    tv.run_ai("rubric")
    wait_for(qapp, lambda: tv.worker is None, timeout=15)
    assert all(r.rubric for r in tv.t.regions) and tv.current_step() == 3

    # "add missing" keeps existing regions and adds nothing new for the same labels
    llm.layout_calls = 0
    tv.start_detect(autosetup=False, replace=False)
    wait_for(qapp, lambda: tv.worker is None, timeout=15)
    assert len(tv.t.regions) == 2 and all(r.rubric for r in tv.t.regions)

    SCREENSHOT_DIR.mkdir(exist_ok=True)
    tv.region_list.setCurrentRow(1)
    qapp.processEvents()
    mw.grab().save(str(SCREENSHOT_DIR / "template_editor_multipage.png"))


def test_add_version_copies_pages(editor, pages):
    tv, _, qapp, _ = editor
    a = tv.create_exam("Versioned", pages)
    tv.set_page(1)
    tv.on_rect_drawn((100, 300, 300, 150))
    tv.flush()
    src = db.load_template(a.id)
    b = tv.create_version("Versioned", "B", list(reversed(pages)), copy_from=src)
    assert b.page_count == 2 and b.regions[0].page == 1
    assert tv.t.id == b.id and tv.t.version_label == "B"
