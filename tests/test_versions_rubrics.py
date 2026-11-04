"""New versions start with the outline of an existing version; rubrics can be redone from scratch."""
import pytest
from PyQt6.QtWidgets import QMessageBox
from lmgrader import db
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist, duplicate_as_version
from conftest import FakeLLM, make_page
from test_template_editor import wait_for


def _exam(tmp_path, name):
    p = str(tmp_path / f"{name}_a.png")
    save_image(p, make_page(91, "VERSION A"))
    t = create_template_from_image(f"{name} vA", p, name, "A")
    for lab, y in (("Q1a", 300), ("Q1b", 600)):
        r = add_region(t, lab, "g", (100, y, 300, 150), 3, rubric="- [3 pt] old rubric")
        r.question_text, r.question_rect, r.grading_notes = "Old question", (100, y - 80, 400, 60), "radians ok"
    t.name_rect, t.id_rect, t.columns = (500, 100, 300, 60), (500, 180, 300, 60), 2
    t.stems = {"1": "stem"}
    t.id = persist(t)
    return t


def test_duplicate_boxes_only(tmp_path):
    t = _exam(tmp_path, "Dup")
    p = str(tmp_path / "b.png"); save_image(p, make_page(92, "VERSION B"))
    b = duplicate_as_version(t, p, "B", keep_text=False)
    assert [r.rect for r in b.regions] == [r.rect for r in t.regions]
    assert [r.problem for r in b.regions] == ["Q1a", "Q1b"] and all(r.points == 3 for r in b.regions)
    assert all(r.grading_notes == "radians ok" and r.question_rect for r in b.regions)
    assert all(not r.question_text and not r.rubric for r in b.regions) and b.stems == {}
    assert (b.name_rect, b.id_rect, b.columns) == (t.name_rect, t.id_rect, 2)


class RubricLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.n = 0

    def generate_rubric(self, q, p, prob, instructions="", notes=""):
        self.n += 1
        return f"- [{p:g} pt] new rubric {self.n}"


@pytest.fixture
def tv(qapp, monkeypatch):
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    llm = RubricLLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    yield mw.template_view, llm
    mw.template_view.flush(); mw.close()


def test_copy_boxes_into_empty_version(tv, qapp, tmp_path):
    v, _ = tv
    t = _exam(tmp_path, "Copyexam")
    p = str(tmp_path / "empty_b.png"); save_image(p, make_page(93, "VERSION B"))
    b = v.create_version("Copyexam", "B", [p])            # created empty (e.g. before this feature)
    assert not b.regions and v.btn_copy_boxes.isVisibleTo(v)
    v.copy_boxes_from_version()
    v.flush()
    nb = db.load_template(b.id)
    assert [r.rect for r in nb.regions] == [r.rect for r in t.regions] and nb.name_rect == t.name_rect
    assert all(not r.rubric and not r.question_text for r in nb.regions)


def test_redo_rubrics(tv, qapp, tmp_path):
    v, llm = tv
    t = _exam(tmp_path, "Redoexam")
    v.refresh_list(t.id); v.load(t.id); qapp.processEvents()
    v.region_list.setCurrentRow(0); qapp.processEvents()
    assert v.btn_rubric.text() == "Redo from scratch" and v.btn_rubric_redo.isEnabled()
    v.run_ai("rubric")                       # "missing rubrics": none missing, nothing happens
    assert v.worker is None and llm.n == 0
    v.btn_rubric_redo.click()
    wait_for(qapp, lambda: v.worker is None)
    assert [r.rubric for r in v.t.regions] == ["- [3 pt] new rubric 1", "- [3 pt] new rubric 2"]
    v.flush()
    assert db.load_template(t.id).regions[1].rubric == "- [3 pt] new rubric 2"
    v.in_rubric.setPlainText("")
    assert v.btn_rubric.text() == "Generate with LLM"
