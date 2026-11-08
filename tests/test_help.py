"""Help: sidebar button / F1, a topic for every tab, follows the tab, respects "Use an LLM"."""
import pytest
from PyQt6.QtGui import QShortcut, QKeySequence
from conftest import SCREENSHOT_DIR


@pytest.fixture
def mw(qapp):
    from lmgrader.ui.main_window import MainWindow
    w = MainWindow(); w.resize(1400, 880); w.show(); qapp.processEvents()
    yield w
    if w._help is not None:
        w._help.close()
    w.template_view.flush()
    w.close()


def test_every_tab_has_help(mw):
    from lmgrader.ui import help_view as hv
    assert len(mw.tab_topics) == mw.stack.count()
    keys = dict(hv.TOPICS)
    for topic in mw.tab_topics:
        assert topic in keys
        for llm in (True, False):
            if topic == "llm" and not llm:
                continue
            html = hv.help_html(topic, llm)
            assert keys[topic].replace("&", "&amp;") in html and len(html) > 300


def test_button_opens_help_on_current_tab_and_follows(qapp, mw):
    assert mw.btn_help.parentWidget() is mw.btn_theme.parentWidget()        # in the sidebar
    mw.nav.setCurrentRow(mw.stack.indexOf(mw.review_view)); qapp.processEvents()
    mw.btn_help.click(); qapp.processEvents()
    h = mw._help
    assert h.isVisible() and h.current_topic() == "review"
    assert "Accept suggestion" in h.text.toPlainText()
    # switching tabs while the help is open: it follows
    mw.nav.setCurrentRow(mw.stack.indexOf(mw.students_view)); qapp.processEvents()
    assert h.current_topic() == "students" and "Import CSV" in h.text.toPlainText()
    # picking another topic in the help works too
    h.show_topic("keys")
    assert "Ctrl+Enter" in h.text.toPlainText()
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    h.show_topic("hand"); qapp.processEvents()
    h.grab().save(str(SCREENSHOT_DIR / "help.png"))
    # closed: switching tabs doesn't reopen it
    h.close(); qapp.processEvents()
    mw.nav.setCurrentRow(mw.stack.indexOf(mw.scan_view)); qapp.processEvents()
    assert not h.isVisible()
    mw.show_help(); qapp.processEvents()
    assert h.isVisible() and h.current_topic() == "scan"


def test_without_llm(qapp, mw, monkeypatch):
    from lmgrader.config import LLM_CONFIG
    from lmgrader.ui import help_view as hv
    monkeypatch.setitem(LLM_CONFIG, "enabled", False)
    mw.apply_llm_mode()
    mw.nav.setCurrentRow(mw.stack.indexOf(mw.template_view)); qapp.processEvents()
    mw.show_help(); qapp.processEvents()
    h = mw._help
    assert "llm" not in h.topic_keys()
    text = h.text.toPlainText()
    assert "Redraw box" in text and "Question marks" not in text and "Auto-setup" not in text
    assert "LLM Grade" not in hv.help_html("start", False)
    # turned back on: LLM Grade comes back
    monkeypatch.setitem(LLM_CONFIG, "enabled", True)
    mw.apply_llm_mode()
    assert "llm" in h.topic_keys()
    h.show_topic("templates")
    assert "Auto-setup with LLM" in h.text.toPlainText()


def test_keys_come_from_review_constants():
    from lmgrader.ui import help_view as hv, review_view as rv
    html = hv.help_html("keys", True)
    for seqs in (rv.KEYS_NEXT_QUESTION, rv.KEYS_NEXT_TEST, rv.KEYS_ACCEPT_NEXT, rv.KEYS_NEXT_FLAGGED):
        assert hv.pretty_keys(seqs) in html
    assert hv.pretty_keys(("Ctrl+Return", "Ctrl+Enter")) == "Ctrl+Enter"
    assert hv.pretty_keys(("Alt+Down",)) == "Alt+↓"


def test_settings_help_connects_lm_studio():
    from lmgrader import config
    from lmgrader.ui import help_view as hv
    for llm in (True, False):
        html = hv.help_html("settings", llm)
        assert "Connect LM Studio" in html and config.DEFAULT_LLM_BASE_URL in html
        for name in ("Use an LLM", "Base URL", "API key", "Load models", "Vision model", "Save"):
            assert f"<b>{name}</b>" in html
        assert "LM Studio" in hv.help_html("start", llm)


def test_f1_registered(mw):
    f1 = QKeySequence(QKeySequence.StandardKey.HelpContents)
    assert any(sc.key() == f1 for sc in mw.findChildren(QShortcut))
