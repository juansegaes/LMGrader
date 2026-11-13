"""Light/dark mode: switching at runtime, persisted in settings.json, screenshots of every page."""
import json
from PyQt6.QtGui import QPalette
from lmgrader import config
from lmgrader.ui import theme
from conftest import SCREENSHOT_DIR


def test_dark_mode_toggle_and_persist(qapp, world):
    from lmgrader.ui.main_window import MainWindow
    mw = MainWindow(); mw.resize(1400, 880); mw.show(); qapp.processEvents()
    try:
        assert theme.mode == "light" and "Dark mode" in mw.btn_theme.text()
        assert mw.brand_logo.property("source") == "lmgrader-logo-light.svg"
        assert mw.brand_logo.renderer().isValid()
        mw.btn_theme.click(); qapp.processEvents()
        assert theme.mode == "dark" and theme.BG == theme.DARK["BG"] and "Light mode" in mw.btn_theme.text()
        assert mw.brand_logo.property("source") == "lmgrader-logo-dark.svg"
        assert mw.brand_logo.renderer().isValid()
        assert qapp.palette().color(QPalette.ColorRole.Window).name() == theme.DARK["BG"]
        assert theme.DARK["SURFACE"] in qapp.styleSheet()
        saved = json.loads(config.settings_path().read_text(encoding="utf-8"))
        assert saved["ui"]["theme"] == "dark"
        assert mw.settings_view.in_theme.currentData() == "dark"
        # the templates page with an exam loaded, plus the other pages
        tv = mw.template_view
        tv.refresh_list(world["ta_id"]); tv.load(world["ta_id"]); qapp.processEvents()
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        for row, name in ((0, "templates"), (2, "scan"), (3, "review"), (5, "settings")):
            mw.nav.setCurrentRow(row); qapp.processEvents()
            mw.grab().save(str(SCREENSHOT_DIR / f"dark_{name}.png"))
        # Settings combo switches back
        s = mw.settings_view
        s.in_theme.setCurrentIndex(s.in_theme.findData("light"))
        s._theme_chosen(s.in_theme.currentIndex()); qapp.processEvents()
        assert theme.mode == "light" and "Dark mode" in mw.btn_theme.text()
        assert mw.brand_logo.property("source") == "lmgrader-logo-light.svg"
        assert theme.resolve_mode("system") in ("light", "dark")
    finally:
        config.UI_CONFIG["theme"] = "light"
        theme.apply_theme(qapp, "light")       # other tests' screenshots stay light
        tv.dirty = False
        mw.close()
