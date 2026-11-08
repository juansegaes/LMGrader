"""Scans load in the background, names are read only on request, the "Use an LLM" switch, and
the exam/version kept when switching tabs."""
import os
import threading
import time

import pytest
from PIL import Image

from conftest import make_page, SHIPPED_LLM_ENABLED
from lmgrader import db
from lmgrader.config import LLM_CONFIG
from lmgrader.models import Submission, SUB_NEW
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, persist


def _wait(qapp, view, timeout=60):
    t0 = time.time()
    while view.worker is not None and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert view.worker is None, "worker did not finish"


def _scan_view(qapp):
    from lmgrader.ui.scan_view import ScanView, STEP_UPLOAD, STEP_NAMES
    v = ScanView(steps=(STEP_UPLOAD, STEP_NAMES))
    v.show()
    v.refresh()
    v.cb_exam.setCurrentIndex(v.cb_exam.findText("Midterm"))
    qapp.processEvents()
    return v


def _new_ids(before):
    return [s.id for s in db.list_submissions_by_status("Midterm") if s.id not in before]


def test_pdf_loads_in_background_and_names_wait_for_the_button(qapp, world, data_dir):
    pdf = str(data_dir / "three_tests.pdf")
    pages = [Image.fromarray(make_page(80 + i, f"STUDENT {i}")[:, :, ::-1].copy()) for i in range(3)]
    pages[0].save(pdf, save_all=True, append_images=pages[1:])
    v = _scan_view(qapp)
    before = {s.id for s in db.list_submissions_by_status("Midterm")}
    seen = []
    orig = v._on_progress
    v._on_progress = lambda i, n, msg: (seen.append(msg), orig(i, n, msg))
    try:
        v.import_files([pdf])
        assert v.worker is not None and v.worker.kind == "import"
        assert v.btn_add.text() == "Loading…" and v.progress.isVisible() and v.btn_stop_job.isVisible()
        _wait(qapp, v)
        assert "Loading three_tests.pdf… page 1 of 3" in seen and "Loading three_tests.pdf… page 3 of 3" in seen
        ids = _new_ids(before)
        assert len(ids) == 3 and all(db.get_submission(i).status == SUB_NEW for i in ids)   # not read
        assert v.worker is None and v.btn_add.text() == "Add files…"
        assert v.names_banner.isVisible() and "3 tests not read yet" in v.lbl_names_banner.text()
        assert "3 to read" in v.step_btns[1].text()
    finally:
        for i in _new_ids(before):
            db.delete_submission(i)
        v.shutdown()
        v.close()


def test_stopping_the_loading_adds_nothing(qapp, world, data_dir, monkeypatch):
    from lmgrader.ui import scan_view
    written, started = [], threading.Event()

    def slow_import(path, progress=None, should_stop=None):
        for i in range(2):
            p = str(data_dir / f"slow_{i}_{time.time_ns()}.png")
            save_image(p, make_page(90 + i, "SLOW"))
            written.append(p)
            progress(i, 5)
        started.set()
        t0 = time.time()
        while not should_stop() and time.time() - t0 < 20:
            time.sleep(0.01)
        return list(written)

    monkeypatch.setattr(scan_view, "import_pages", slow_import)
    v = _scan_view(qapp)
    before = {s.id for s in db.list_submissions_by_status("Midterm")}
    try:
        v.import_files(["big.pdf"])
        t0 = time.time()
        while not started.is_set() and time.time() - t0 < 20:
            qapp.processEvents()
            time.sleep(0.01)
        v.stop()
        _wait(qapp, v)
        assert written and not any(os.path.exists(p) for p in written)
        assert not _new_ids(before) and "nothing was added" in v.lbl_progress.text()
    finally:
        v.shutdown()
        v.close()


def test_llm_is_off_by_default():
    assert SHIPPED_LLM_ENABLED is False


def test_openrouter_is_off_grading_stays_local(qapp, monkeypatch):
    from lmgrader import config
    from lmgrader.llm.client import LLMClient
    from lmgrader.ui.settings_view import SettingsView
    assert config.OPENROUTER_ENABLED is False
    monkeypatch.setitem(LLM_CONFIG, "grading_provider", "openrouter")     # e.g. saved before
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert LLMClient.for_grading().base_url == LLM_CONFIG["base_url"]
    v = SettingsView()
    assert v.grading_box.isHidden() and "OpenRouter" not in v.chk_llm.text()
    monkeypatch.setattr(config, "OPENROUTER_ENABLED", True)              # the code is still there
    assert LLMClient.for_grading().base_url == config.OPENROUTER_BASE_URL
    assert not SettingsView().grading_box.isHidden()


def test_no_llm_switch_hides_the_ai_and_never_calls_it(qapp, world, monkeypatch):
    from lmgrader.llm.client import LLMClient, LLMDisabled
    from lmgrader.ui import scan_view
    from lmgrader.ui.main_window import MainWindow
    monkeypatch.setitem(LLM_CONFIG, "enabled", False)
    with pytest.raises(LLMDisabled):
        LLMClient.from_config()
    with pytest.raises(LLMDisabled):
        LLMClient.for_grading()

    calls = []
    monkeypatch.setattr(scan_view, "job_identify",
                        lambda ids, versions, students, use_llm, worker=None: calls.append(use_llm))
    sub = Submission(exam_name="Midterm", status=SUB_NEW, scan_pages=[world["scan"]])
    db.add_submission(sub)
    mw = MainWindow()
    try:
        assert mw.nav.isRowHidden(mw.stack.indexOf(mw.llm_view))
        tv = mw.template_view
        tv.load(world["ta_id"])
        for w in (tv.btn_autosetup, tv.btn_detect, tv.btn_extract, tv.btn_rubric, tv.btn_rubric_all,
                  tv.ai_box, tv.notes_box, tv.columns_box):
            assert w.isHidden()
        assert "Draw answer boxes" in tv.lbl_steps.text()
        sv = mw.scan_view
        sv.cb_exam.setCurrentIndex(sv.cb_exam.findText("Midterm"))
        assert sv.chk_names.isHidden()
        sv.chk_names.setChecked(True)          # even if it was ticked before, no LLM is asked
        sv.read_names()
        _wait(qapp, sv)
        assert calls == [False]

        # Settings → Use an LLM, Save: everything comes back
        s = mw.settings_view
        s.chk_llm.setChecked(True)
        s.save()
        assert LLM_CONFIG["enabled"] and not mw.nav.isRowHidden(mw.stack.indexOf(mw.llm_view))
        assert not tv.btn_autosetup.isHidden() and not tv.ai_box.isHidden()
        s.chk_llm.setChecked(False)
        assert not s.llm_card.isEnabled()
    finally:
        mw.template_view.dirty = False
        mw.close()
        db.delete_submission(sub.id)


def test_switching_tabs_keeps_the_exam_and_version(qapp, world, data_dir):
    from lmgrader.ui.main_window import MainWindow
    p = str(data_dir / "ctx.png")
    save_image(p, make_page(95, "CTX"))
    other = create_template_from_image("Ctx vA", p, "Ctx exam", "A")
    other.id = persist(other)
    mw = MainWindow()
    rows = {mw.nav.item(i).text(): i for i in range(mw.nav.count())}

    def go(tab):
        mw.nav.setCurrentRow(rows[tab])
        qapp.processEvents()

    try:
        tv, hv, sv = mw.template_view, mw.hand_view, mw.scan_view
        tv.load(other.id)
        go("Hand Grade")
        assert hv.cb_exam.currentText() == "Ctx exam" and hv.cb_version.currentData() == other.id
        go("Exams & Templates")
        tv.load(world["tb_id"])                         # Midterm, version B
        go("Hand Grade")
        assert hv.cb_exam.currentText() == "Midterm" and hv.cb_version.currentData() == world["tb_id"]
        go("Scan & Names")                              # same exam; detection stays on Auto-detect
        assert sv.exam == "Midterm" and sv.cb_version.currentData() is None
        go("Hand Grade")                                # ...and the version B is not forgotten
        assert hv.cb_version.currentData() == world["tb_id"]
        hv.cb_version.setCurrentIndex(hv.cb_version.findData(world["ta_id"]))
        go("Exams & Templates")
        assert tv.t.id == world["ta_id"]
        kind, val = tv.tree.currentItem().data(0, 0x0100)
        assert (kind, val) == ("version", world["ta_id"])
        go("Review")
        assert mw.review_view.cb_exam.currentData() == "Midterm"
        go("Return")
        assert mw.return_view.cb_exam.currentText() == "Midterm"
        go("LLM Grade")
        assert mw.llm_view.exam == "Midterm"
    finally:
        mw.template_view.dirty = False
        mw.close()
        db.delete_template(other.id)
