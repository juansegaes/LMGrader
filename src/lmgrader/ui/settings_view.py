"""Settings view."""
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QMessageBox, QComboBox, QFormLayout,
                             QSpinBox, QDoubleSpinBox, QPlainTextEdit, QScrollArea, QCheckBox)
from PyQt6.QtCore import Qt
from ..config import (LLM_CONFIG, SMTP_CONFIG, UI_CONFIG, save_settings, settings_path, clamp_strictness,
                      STRICTNESS_MIN, STRICTNESS_MAX)
from ..llm import LLMClient
from ..grading.grader import reapply_full_marks_rules
from ..llm.client import STRICTNESS_NAMES
from ..returns import keyring_available
from .theme import button, card, label, setup_page

class SettingsView(QWidget):
    def __init__(self):
        super().__init__()
        body, actions = setup_page(self, "Settings", "Local LLM server (LM Studio or any OpenAI-compatible API).")
        self.btn_save = button("Save", "primary")
        actions.addWidget(self.btn_save)

        # ---- Appearance (applied immediately) ----
        ac, al = card(margins=18)
        al.addWidget(label("Appearance", "sectionTitle"))
        af = QFormLayout()
        self.in_theme = QComboBox()
        for text, value in (("Light", "light"), ("Dark", "dark"), ("Follow Windows", "system")):
            self.in_theme.addItem(text, value)
        self.sync_theme()
        self.in_theme.setMaximumWidth(220)
        af.addRow("Theme", self.in_theme)
        al.addLayout(af)
        ac.setMaximumWidth(820)
        self.in_theme.activated.connect(self._theme_chosen)

        c, cl = card(margins=18)
        cl.addWidget(label("Connection", "sectionTitle"))
        form = QFormLayout()
        form.setSpacing(10)
        self.in_url = QLineEdit(LLM_CONFIG["base_url"])
        self.in_key = QLineEdit(LLM_CONFIG["api_key"])
        self.in_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        mrow = QHBoxLayout()
        self.in_model = QComboBox(); self.in_model.setEditable(True)
        self.in_model.addItem(LLM_CONFIG["model"])
        self.in_model.setMinimumWidth(320)
        self.btn_list = button("Load models")
        mrow.addWidget(self.in_model, 1); mrow.addWidget(self.btn_list)
        form.addRow("Base URL", self.in_url)
        form.addRow("API key", self.in_key)
        form.addRow("Vision model", mrow)
        cl.addLayout(form)
        cl.addWidget(label("Use a vision-capable model (e.g. Qwen2.5-VL, Gemma 3, Llama 3.2 Vision).", "muted"))

        cl.addSpacing(8)
        cl.addWidget(label("Generation", "sectionTitle"))
        form2 = QFormLayout()
        self.in_tokens = QSpinBox(); self.in_tokens.setRange(256, 32768); self.in_tokens.setSingleStep(256)
        self.in_tokens.setValue(int(LLM_CONFIG.get("max_tokens", 2048)))
        self.in_temp = QDoubleSpinBox(); self.in_temp.setRange(0, 2); self.in_temp.setSingleStep(0.05)
        self.in_temp.setValue(float(LLM_CONFIG.get("temperature", 0.1)))
        form2.addRow("Max tokens", self.in_tokens)
        form2.addRow("Temperature", self.in_temp)
        cl.addLayout(form2)
        cl.addWidget(label("Reasoning models need more tokens (4096+).", "muted"))

        cl.addSpacing(8)
        cl.addWidget(label("Human grading", "sectionTitle"))
        form3 = QFormLayout()
        self.in_conf = QSpinBox(); self.in_conf.setRange(0, 100); self.in_conf.setSuffix(" %")
        self.in_conf.setSpecialValueText("off")
        self.in_conf.setValue(round(float(LLM_CONFIG.get("review_confidence", 0.7) or 0) * 100))
        self.in_conf.setToolTip("Questions the AI grades with lower confidence are left for you to grade. 0 = off.")
        form3.addRow("Send to human below confidence", self.in_conf)
        self.in_full_skip = QCheckBox("Full marks from the AI: don't send to a human unless confidence is below")
        self.in_full_skip.setChecked(bool(LLM_CONFIG.get("full_marks_skip_review", False)))
        self.in_full_skip.setToolTip("When the AI gives a question 100% of its points, the confidence rule above "
                                     "only applies below this lower threshold. Other reasons (illegible, work "
                                     "continues elsewhere, back page) still send it to a human.")
        self.in_full_floor = QSpinBox(); self.in_full_floor.setRange(0, 100); self.in_full_floor.setSuffix(" %")
        self.in_full_floor.setValue(round(float(LLM_CONFIG.get("full_marks_min_confidence", 0.3) or 0) * 100))
        self.in_full_floor.setEnabled(self.in_full_skip.isChecked())
        self.in_full_skip.toggled.connect(self.in_full_floor.setEnabled)
        frow = QHBoxLayout(); frow.setSpacing(8)
        frow.addWidget(self.in_full_skip); frow.addWidget(self.in_full_floor); frow.addStretch()
        form3.addRow("", frow)
        self.in_full_back = QCheckBox("Full marks from the AI: writing on the back page doesn't need a human")
        self.in_full_back.setChecked(bool(LLM_CONFIG.get("full_marks_ignore_back", True)))
        self.in_full_back.setToolTip("Writing detected on the back page normally sends the questions it may "
                                     "belong to to a human. With full marks there is nothing more to award, "
                                     "so those questions (and the test) are not flagged for it.")
        form3.addRow("", self.in_full_back)
        self.in_instructions = QPlainTextEdit(LLM_CONFIG.get("default_instructions", ""))
        self.in_instructions.setPlaceholderText("e.g. Make sure all work is shown; deduct points for answers "
                                                "without supporting work.")
        self.in_instructions.setMaximumHeight(90)
        form3.addRow("Default grading instructions\nfor new exams", self.in_instructions)
        self.in_strictness = QComboBox()
        for lvl in range(STRICTNESS_MAX, STRICTNESS_MIN - 1, -1):
            self.in_strictness.addItem(f"{lvl} · {STRICTNESS_NAMES[lvl]}", lvl)
        self.in_strictness.setCurrentIndex(max(0, self.in_strictness.findData(
            clamp_strictness(LLM_CONFIG.get("default_strictness", 10)))))
        self.in_strictness.setToolTip("Strictness pre-set for new exams; each exam can change it in "
                                      "Exams & Templates.")
        form3.addRow("Default strictness\nfor new exams", self.in_strictness)
        cl.addLayout(form3)
        lbl = label("Answers that say the work continues elsewhere (\"see back\", \"ver atrás\"…) always go "
                    "to a human, as do answers the AI flags as illegible or ambiguous.", "muted")
        lbl.setWordWrap(True)
        cl.addWidget(lbl)
        cl.addWidget(label(f"Saved to {settings_path()}", "muted"))
        c.setMaximumWidth(820)

        # ---- Email (SMTP) for returning tests ----
        ec, el = card(margins=18)
        el.addWidget(label("Email (SMTP)", "sectionTitle"))
        el.addWidget(label("Used by Return to email each student their annotated test. "
                           "Gmail: smtp.gmail.com, 465, SSL, with an App Password (needs 2-step verification). "
                           "Outlook/Office 365: smtp.office365.com, 587, STARTTLS (your school may have disabled it).",
                           "muted"))
        el.itemAt(el.count() - 1).widget().setWordWrap(True)
        ef = QFormLayout(); ef.setSpacing(10)
        s = SMTP_CONFIG
        self.in_host = QLineEdit(s["host"])
        self.in_port = QSpinBox(); self.in_port.setRange(1, 65535); self.in_port.setValue(int(s["port"]))
        self.in_sec = QComboBox(); self.in_sec.addItems(["SSL", "STARTTLS", "none"])
        self.in_sec.setCurrentText(s["security"])
        self.in_user = QLineEdit(s["username"]); self.in_user.setPlaceholderText("you@school.edu")
        self.in_from_name = QLineEdit(s["from_name"]); self.in_from_name.setPlaceholderText("Shown as the sender name")
        self.in_from = QLineEdit(s["from_address"]); self.in_from.setPlaceholderText("defaults to the username")
        self.in_delay = QDoubleSpinBox(); self.in_delay.setRange(0, 60); self.in_delay.setSuffix(" s")
        self.in_delay.setValue(float(s["delay"]))
        self.in_subject = QLineEdit(s["subject"])
        self.in_body = QPlainTextEdit(s["body"]); self.in_body.setMinimumHeight(110)
        hp = QHBoxLayout(); hp.addWidget(self.in_host, 1); hp.addWidget(self.in_port); hp.addWidget(self.in_sec)
        ef.addRow("Server", hp)
        ef.addRow("Username", self.in_user)
        ef.addRow("From name", self.in_from_name)
        ef.addRow("From address", self.in_from)
        ef.addRow("Delay between emails", self.in_delay)
        ef.addRow("Subject", self.in_subject)
        ef.addRow("Message", self.in_body)
        el.addLayout(ef)
        el.addWidget(label("Placeholders: {name} {first_name} {exam} {version} {total} {max} {percent}. "
                           "The password is asked for when sending" +
                           (" and can be stored in the system keyring." if keyring_available()
                            else " and kept only until LMGrader closes (never saved to disk)."), "muted"))
        el.itemAt(el.count() - 1).widget().setWordWrap(True)
        self.in_sec.currentTextChanged.connect(
            lambda t: self.in_port.setValue({"SSL": 465, "STARTTLS": 587}.get(t, self.in_port.value())))
        ec.setMaximumWidth(820)

        inner = QWidget(); il = QVBoxLayout(inner); il.setContentsMargins(0, 0, 0, 0); il.setSpacing(14)
        inner.setObjectName("page"); inner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        il.addWidget(ac); il.addWidget(c); il.addWidget(ec); il.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(inner)
        body.addWidget(scroll, 1)

        self.btn_save.clicked.connect(self.save)
        self.btn_list.clicked.connect(self.list_models)

    def sync_theme(self):
        i = self.in_theme.findData(UI_CONFIG.get("theme", "light"))
        self.in_theme.setCurrentIndex(max(0, i))

    def _theme_chosen(self, _index):
        choice = self.in_theme.currentData()
        win = self.window()
        if hasattr(win, "set_theme"):
            win.set_theme(choice)
        else:   # standalone (tests)
            from PyQt6.QtWidgets import QApplication
            from . import theme
            UI_CONFIG["theme"] = choice
            save_settings()
            theme.apply_theme(QApplication.instance(), choice)

    def save(self):
        LLM_CONFIG["base_url"] = self.in_url.text().strip()
        LLM_CONFIG["api_key"] = self.in_key.text().strip()
        LLM_CONFIG["model"] = self.in_model.currentText().strip()
        LLM_CONFIG["max_tokens"] = self.in_tokens.value()
        LLM_CONFIG["temperature"] = self.in_temp.value()
        LLM_CONFIG["review_confidence"] = self.in_conf.value() / 100
        LLM_CONFIG["full_marks_skip_review"] = self.in_full_skip.isChecked()
        LLM_CONFIG["full_marks_min_confidence"] = self.in_full_floor.value() / 100
        LLM_CONFIG["full_marks_ignore_back"] = self.in_full_back.isChecked()
        LLM_CONFIG["default_instructions"] = self.in_instructions.toPlainText().strip()
        LLM_CONFIG["default_strictness"] = self.in_strictness.currentData()
        SMTP_CONFIG.update(host=self.in_host.text().strip(), port=self.in_port.value(),
                           security=self.in_sec.currentText(), username=self.in_user.text().strip(),
                           from_name=self.in_from_name.text().strip(), from_address=self.in_from.text().strip(),
                           delay=self.in_delay.value(), subject=self.in_subject.text(),
                           body=self.in_body.toPlainText())
        save_settings()
        cleared = reapply_full_marks_rules()   # the full-marks options also cover tests already graded
        msg = "Settings saved."
        if cleared:
            msg += f" {cleared} full-marks question{'s' if cleared != 1 else ''} no longer need a human."
        self.window().statusBar().showMessage(msg, 5000)

    def list_models(self):
        c = LLMClient(base_url=self.in_url.text().strip(), api_key=self.in_key.text().strip())
        models = c.list_models()
        if not models:
            QMessageBox.warning(self, "Models", "No models returned. Is LM Studio running with the server enabled?")
            return
        cur = self.in_model.currentText()
        self.in_model.clear()
        self.in_model.addItems(models)
        if cur in models:
            self.in_model.setCurrentText(cur)
        self.window().statusBar().showMessage(f"{len(models)} models available.", 3000)
