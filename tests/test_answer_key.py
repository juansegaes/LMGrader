"""Answer keys written from the exam's .tex (JSON) imported into a version, without the vision LLM."""
import json
import re
import subprocess
import sys
import pytest
from lmgrader import db
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist, question_context
from lmgrader.template.answer_key import (parse_answer_key, apply_answer_key, match_key, SKILL_PATH,
                                          describe)
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

KEY = {
    "format": "lmgrader-answer-key", "version": 1, "exam": "Key quiz", "exam_version": "A",
    "instructions": "No credit without supporting work.",
    "questions": [
        {"label": "1", "stem": r"Let $A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}$.",
         "parts": [
             {"label": "a", "text": "Compute $A^{-1}$.", "answer": r"$\frac{1}{-2}(\dots)$",
              "rubric": [{"points": 1, "criterion": r"$\det A = -2$"}, "[2 pt] correct inverse"]},
             {"label": "(b)", "text": "Solve $Ax=b$.", "points": 3, "answer": "$x=(-2, 3/2)$",
              "notes": "Follow-through from (a)."}]},
        {"label": "2", "type": "True/False", "points": 2, "text": "Every invertible matrix is diagonalizable.",
         "answer": "False"},
        {"label": "3", "type": "multiple_choice", "points": 2, "text": "Pick a basis. (A) ... (B) ...",
         "answer": "(B)"},
        {"label": "4", "text": "Not on the printed exam."},
    ]}


def _template(tmp_path, name):
    p = str(tmp_path / f"{name}.png")
    save_image(p, make_page(81, "KEY"))
    t = create_template_from_image(f"{name} vA", p, name, "A")
    for lab, y in (("Q1 (a)", 200), ("Q1 (b)", 400), ("Q2", 600), ("Q3", 800), ("Bonus", 950)):
        add_region(t, lab, "g", (100, y, 300, 100), 1)
    return t


def test_parse():
    key = parse_answer_key(KEY)
    assert [it.label for it in key.items] == ["1a", "1b", "2", "3", "4"]
    a, b, tf, mc, _ = key.items
    assert a.points == 3 and a.rubric == [(1.0, r"$\det A = -2$"), (2.0, "correct inverse")]   # sum of rubric
    assert a.stem == b.stem and b.stem.startswith("Let $A")
    assert tf.type == "true_false" and mc.type == "multiple_choice"
    assert match_key("Q1 (a)") == match_key("1a") and match_key("Bonus") == "bonus"
    for bad, msg in (({"questions": [{"text": "x"}]}, "no \"label\""),
                     ({"questions": [{"label": "1"}, {"label": "Q1"}]}, "twice"),
                     ({"questions": [{"label": "1", "type": "essay"}]}, "unknown type"),
                     ({"questions": [{"label": "1", "points": "four"}]}, "must be a number"),
                     ({"questions": [{"label": "1", "rubric": ["correct"]}]}, "needs points"),
                     ({"format": "other", "questions": []}, "Unknown format"),
                     ("{not json", "Not valid JSON")):
        with pytest.raises(ValueError, match=msg):
            parse_answer_key(bad)


def test_apply(tmp_path):
    t = _template(tmp_path, "Key apply")
    t.regions[3].question_text, t.regions[3].rubric = "old OCR text", "- [1 pt] old"   # Q3: replaced
    t.regions[4].rubric = "- [5 pt] kept"                                            # Bonus: not in key
    rep = apply_answer_key(t, parse_answer_key(KEY))
    a, b, tf, mc, bonus = t.regions
    assert rep.matched == ["Q1 (a)", "Q1 (b)", "Q2", "Q3"] and rep.no_box == ["4"] and rep.no_key == ["Bonus"]
    assert t.stems == {"1": KEY["questions"][0]["stem"]}
    assert a.question_text == "Compute $A^{-1}$." and a.points == 3 and a.rubric.startswith("Expected answer:")
    assert "- [1 pt] $\\det A = -2$" in a.rubric and "- [2 pt] correct inverse" in a.rubric
    assert b.points == 3 and b.answer_key == "$x=(-2, 3/2)$" and b.rubric == "" and b.grading_notes
    assert "Main question 1" in question_context(t, 1)
    assert tf.rubric == ("Expected answer: False\n\nCriteria:\n"
                         "- [2 pt] Answers False (all or nothing: no partial credit)")
    assert "(B)" in mc.rubric and rep.rubrics_from_key == ["Q1 (a)", "Q2", "Q3"]
    assert rep.replaced == ["Q3"] and bonus.rubric == "- [5 pt] kept"
    # re-importing the same key changes nothing; an LLM rubric written for the same text survives
    b.rubric = "- [3 pt] llm rubric"
    rep2 = apply_answer_key(t, parse_answer_key(KEY))
    assert b.rubric == "- [3 pt] llm rubric" and not rep2.replaced
    # a key with a new answer makes that rubric stale
    k = json.loads(json.dumps(KEY)); k["questions"][0]["parts"][1]["answer"] = "$x=0$"
    apply_answer_key(t, parse_answer_key(k))
    assert b.rubric == ""
    # persisted with the template
    t.id = persist(t)
    assert db.load_template(t.id).regions[1].answer_key == "$x=0$"


def test_apply_without_rubrics(tmp_path):
    """Rubrics not imported: questions/points/answers are, and the rubrics are left to the LLM."""
    t = _template(tmp_path, "Key norubric")
    t.regions[2].rubric = "- [2 pt] old, for other text"
    rep = apply_answer_key(t, parse_answer_key(KEY), rubrics=False)
    a, b, tf, mc, _ = t.regions
    assert not rep.rubrics_from_key and all(r.rubric == "" for r in (a, b, tf, mc))   # stale one cleared
    assert a.points == 3 and a.answer_key and tf.answer_key == "False" and a.question_text


def test_apply_replace(tmp_path):
    """Re-importing an edited key with replace=True: notes/answers the key no longer has are
    cleared (also on boxes the key doesn't cover); without replace they stay."""
    t = _template(tmp_path, "Key replace")
    apply_answer_key(t, parse_answer_key(KEY))
    a, b, tf, mc, bonus = t.regions
    b.rubric = "- [3 pt] llm rubric"
    bonus.grading_notes, bonus.answer_key = "typed by hand", "42"
    k = json.loads(json.dumps(KEY))
    del k["questions"][0]["parts"][1]["notes"]          # the edited key: no notes
    del k["questions"][2]["answer"]                     # ... and Q3 lost its answer
    rep = apply_answer_key(t, parse_answer_key(k))      # merge: nothing removed
    assert b.grading_notes and bonus.grading_notes and mc.answer_key and not rep.notes_cleared
    rep = apply_answer_key(t, parse_answer_key(k), replace=True)
    assert rep.notes_cleared == ["Q1 (b)", "Bonus"] and rep.answers_cleared == ["Q3", "Bonus"]
    assert b.grading_notes == "" and bonus.grading_notes == "" and bonus.answer_key == ""
    assert mc.answer_key == ""
    assert b.answer_key == "$x=(-2, 3/2)$" and b.rubric == "- [3 pt] llm rubric"   # same answer: rubric kept
    assert "Notes removed (not in the key): Q1 (b), Bonus" in rep.summary()


def test_skill_example_is_valid():
    """The example in the Claude Code skill must stay importable."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---\nname: lmgrader-answer-key\n")
    example = re.search(r"```json\n(.*?)```", text, re.S).group(1)
    key = parse_answer_key(example)
    assert [it.label for it in key.items] == ["1a", "1b", "2", "3"]
    assert key.items[0].points == 4 and key.items[2].type == "true_false"
    assert all(it.rubric for it in key.items if it.type == "open")       # the example follows step 4
    assert all(sum(p for p, _ in it.rubric) == it.points for it in key.items if it.rubric)
    assert "Check every computable answer with Python" in text and "sympy" in text.lower()


def test_cli(tmp_path):
    f = tmp_path / "exam.lmgrader.json"
    f.write_text(json.dumps(KEY), encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "lmgrader.template.answer_key", str(f)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "1a" in out.stdout and "all-or-nothing" in out.stdout
    assert "no answer" in describe(parse_answer_key(KEY))          # question 4
    f.write_text('{"questions": [{"text": "x"}]}', encoding="utf-8")
    out = subprocess.run([sys.executable, "-m", "lmgrader.template.answer_key", str(f)],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "Invalid answer key" in out.stderr


def test_rubric_uses_answer_key():
    from lmgrader.llm.client import LLMClient
    c = LLMClient.__new__(LLMClient)
    sent = []
    c._chat = lambda m, *a, **k: (sent.append(m[1]["content"]),
                                  '{"expected_answer": "x", "criteria": [{"description": "x", "points": 3}]}')[1]
    c.generate_rubric("Solve $Ax=b$.", 3, "1b", answer_key="$x=(-2, 3/2)$")
    assert "answer key for this question:\n$x=(-2, 3/2)$" in sent[-1] and "First solve" not in sent[-1]
    c.generate_rubric("Solve $Ax=b$.", 3, "1b")
    assert "answer key" not in sent[-1] and "First solve" in sent[-1]


class KeyLLM(FakeLLM):
    def __init__(self):
        super().__init__("")
        self.keys = {}

    def generate_rubric(self, q, p, prob, instructions="", notes="", answer_key=""):
        self.keys[prob] = answer_key
        return f"- [{p:g} pt] from key"


def test_import_dialog_checkbox(qapp):
    """The real dialog: the rubric checkbox (ticked by default) decides; Cancel imports nothing."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from lmgrader.ui.template_editor import TemplateEditorView
    v = TemplateEditorView()

    def answer(untick, button, shot=False):
        def go():
            box = QApplication.activeModalWidget()
            if box is None:
                QTimer.singleShot(20, go); return
            assert box.checkBox().isChecked()
            if shot:
                SCREENSHOT_DIR.mkdir(exist_ok=True)
                box.grab().save(str(SCREENSHOT_DIR / "answer_key_import_dialog.png"))
            if untick:
                box.checkBox().setChecked(False)
            box.button(button).click()
        QTimer.singleShot(0, go)

    answer(False, QMessageBox.StandardButton.Yes, shot=True)
    assert v._confirm_key_import("Matched 4 question(s): Q1 (a), Q1 (b), Q2, Q3\nRubrics in the key: Q1 (a), Q2, Q3", 3) is True
    answer(True, QMessageBox.StandardButton.Yes)
    assert v._confirm_key_import("Matched 1", 1) is False
    answer(False, QMessageBox.StandardButton.Cancel)
    assert v._confirm_key_import("Matched 1", 1) is None
    v.close()


def test_editor_import(qapp, tmp_path, monkeypatch):
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    from test_template_editor import wait_for
    llm = KeyLLM()
    monkeypatch.setattr(te.LLMClient, "from_config", classmethod(lambda cls: llm))
    asked, choice = [], [True]          # choice[0]: tick "Import the rubrics from the key"
    monkeypatch.setattr(te.TemplateEditorView, "_confirm_key_import",
                        lambda self, msg, n: (asked.append((msg, n)), choice[0])[1])
    f = tmp_path / "quiz.lmgrader.json"
    f.write_text(json.dumps(KEY), encoding="utf-8")
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        t = _template(tmp_path, "Key editor")
        t.id = persist(t)
        tv.refresh_list(t.id); tv.load(t.id)
        # first without the key's rubrics, then with them
        choice[0] = False
        tv.import_answer_key(str(f))
        msg, n = asked[-1]
        assert n == 3 and "Matched 4 question(s)" in msg and "no box on the template: 4" in msg
        assert "sets the exam's grading instructions" in msg
        assert not any(r.rubric for r in tv.t.regions) and tv.t.regions[0].question_text
        choice[0] = True
        tv.import_answer_key(str(f))
        assert tv.t.regions[0].rubric.startswith("Expected answer:") and not tv.t.regions[1].rubric
        assert tv.current_instructions() == KEY["instructions"]
        tv.flush()
        saved = db.load_template(t.id)
        assert saved.regions[0].question_text == "Compute $A^{-1}$." and saved.stems["1"].startswith("Let $A")
        assert db.get_exam_instructions("Key editor") == KEY["instructions"]
        # 1b still needs a rubric and the LLM gets the key's answer; Bonus (not in the key) works as before
        tv.run_ai("rubric")
        wait_for(qapp, lambda: tv.worker is None)
        assert llm.keys == {"Q1 (b)": "$x=(-2, 3/2)$", "Bonus": ""}
        tv.region_list.setCurrentRow(1); qapp.processEvents()
        assert tv.lbl_answer_key.isVisibleTo(tv) and "$x=(-2, 3/2)$" in tv.lbl_answer_key.text()
        assert tv.in_rubric.toPlainText() == "- [3 pt] from key"
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        mw.grab().save(str(SCREENSHOT_DIR / "template_editor_answer_key.png"))
    finally:
        tv.flush(); mw.close()


def test_editor_reimport_replace(qapp, tmp_path, monkeypatch):
    """Importing an edited key asks Replace/Keep only when something would be cleared."""
    from lmgrader.ui import template_editor as te
    from lmgrader.ui.main_window import MainWindow
    monkeypatch.setattr(te.TemplateEditorView, "_confirm_key_import", lambda self, msg, n: True)
    asked, choice = [], [True]
    monkeypatch.setattr(te.TemplateEditorView, "_ask_replace",
                        lambda self, rep: (asked.append(rep), choice[0])[1])
    f, f2 = tmp_path / "quiz.lmgrader.json", tmp_path / "quiz2.lmgrader.json"
    f.write_text(json.dumps(KEY), encoding="utf-8")
    k = json.loads(json.dumps(KEY)); del k["questions"][0]["parts"][1]["notes"]
    f2.write_text(json.dumps(k), encoding="utf-8")
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    tv = mw.template_view
    try:
        t = _template(tmp_path, "Key reimport")
        t.id = persist(t)
        tv.refresh_list(t.id); tv.load(t.id)
        tv.import_answer_key(str(f)); tv.import_answer_key(str(f))
        assert not asked and tv.t.regions[1].grading_notes        # nothing to clear: no question
        choice[0] = False
        tv.import_answer_key(str(f2))
        assert asked[-1].notes_cleared == ["Q1 (b)"] and tv.t.regions[1].grading_notes   # kept
        choice[0] = None
        tv.import_answer_key(str(f2))
        assert tv.t.regions[1].grading_notes                      # cancelled
        choice[0] = True
        tv.import_answer_key(str(f2))
        assert tv.t.regions[1].grading_notes == ""
        tv.flush()
        assert db.load_template(t.id).regions[1].grading_notes == ""
    finally:
        tv.flush(); mw.close()


def test_ask_replace_dialog(qapp):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from lmgrader.template.answer_key import ImportReport
    from lmgrader.ui.template_editor import TemplateEditorView
    v = TemplateEditorView()
    rep = ImportReport(notes_cleared=["Q5"], answers_cleared=["Q2"])

    def click(text, shot=False):
        def go():
            box = QApplication.activeModalWidget()
            if box is None:
                QTimer.singleShot(20, go); return
            assert "Notes: Q5" in box.text() and "Expected answers: Q2" in box.text()
            if shot:
                SCREENSHOT_DIR.mkdir(exist_ok=True)
                box.grab().save(str(SCREENSHOT_DIR / "answer_key_replace_dialog.png"))
            next(b for b in box.buttons() if b.text().startswith(text)).click()
        QTimer.singleShot(0, go)

    click("Replace", shot=True); assert v._ask_replace(rep) is True
    click("Keep"); assert v._ask_replace(rep) is False
    click("Cancel"); assert v._ask_replace(rep) is None
    v.close()
