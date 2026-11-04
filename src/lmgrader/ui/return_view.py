"""Return graded tests: email them (SMTP) or export for an LMS."""
import os
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QCheckBox, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView, QSplitter, QPlainTextEdit,
                             QLineEdit, QMessageBox, QFileDialog, QProgressBar, QDialog, QFormLayout,
                             QDialogButtonBox, QScrollArea)
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QColor, QDesktopServices
from ..config import SMTP_CONFIG, CANVAS_CONFIG, save_settings
from ..db import list_exams, was_emailed, was_returned
from ..returns import (collect_items, ensure_pdf, render, send_batch, send_test, get_password, set_password,
                       keyring_available, export_lms, PRESETS)
from ..returns import canvas
from ..returns.lms import DEFAULT_PATTERN, filename_for
from . import theme
from .theme import button, card, label, setup_page
from .widgets import Worker

COL_STUDENT, COL_EMAIL, COL_SCORE, COL_REVIEW, COL_RETURN = range(5)

class PasswordDialog(QDialog):
    def __init__(self, username, parent=None, title="SMTP password", prompt=None, field="Password"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        f = QFormLayout(self)
        f.addRow(label(prompt or (f"Password for {username or 'the SMTP server'}.\n"
                                  "Gmail/Outlook need an app password (requires 2-step verification)."), "muted"))
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow(field, self.pw)
        self.remember = QCheckBox("Remember in the system keyring")
        self.remember.setEnabled(keyring_available())
        if not keyring_available():
            self.remember.setToolTip("Install the 'keyring' package to remember it. "
                                     "Otherwise it is kept only until LMGrader closes.")
        f.addRow(self.remember)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        f.addRow(bb)

# ---- worker jobs ----
def job_send(items, settings, password, dry_run, worker=None):
    return send_batch(items, settings, password, progress=worker.progress.emit,
                      should_stop=lambda: worker.cancelled, dry_run=dry_run, on_result=worker.item.emit)

def job_test(settings, password, to_addr, item, worker=None):
    worker.progress.emit(0, 1, f"Sending test to {to_addr}…")
    send_test(settings, password, to_addr, item)
    return to_addr

def job_lms(exam, out_dir, preset, pattern, items, worker=None):
    return export_lms(exam, out_dir, preset, pattern, items=items, progress=worker.progress.emit)

def job_canvas_courses(url, token, worker=None):
    worker.progress.emit(0, 0, "Connecting to Canvas…")
    c = canvas.CanvasClient(url, token)
    return c.whoami().get("name", ""), c.list_courses()

def job_canvas_assignments(url, token, course_id, worker=None):
    worker.progress.emit(0, 0, "Loading assignments…")
    return course_id, canvas.CanvasClient(url, token).list_assignments(course_id)

def job_canvas_match(url, token, course_id, items, worker=None):
    worker.progress.emit(0, 0, "Loading the Canvas roster…")
    return canvas.match_students(items, canvas.CanvasClient(url, token).list_students(course_id))

def job_canvas_upload(url, token, course_id, assignment_id, comment, matches, resend, worker=None):
    return canvas.upload_batch(matches, canvas.CanvasClient(url, token), course_id, assignment_id, comment,
                               progress=worker.progress.emit, should_stop=lambda: worker.cancelled,
                               resend=resend, on_result=worker.item.emit)

class ReturnView(QWidget):
    def __init__(self):
        super().__init__()
        body, actions = setup_page(self, "Return",
                                   "Send each student their annotated test by email, or export it for your LMS.")
        self.items = []
        self.worker = None

        bar_card, bar = card(QHBoxLayout, margins=10)
        bar.addWidget(label("Exam"))
        self.cb_exam = QComboBox(); self.cb_exam.setMinimumWidth(220)
        bar.addWidget(self.cb_exam)
        bar.addSpacing(10)
        self.chk_review = QCheckBox("Include tests that still need review")
        bar.addWidget(self.chk_review)
        bar.addStretch()
        self.lbl_warn = label("", "muted")
        self.lbl_warn.setStyleSheet(f"color: {theme.WARN}; font-weight: 600;")
        bar.addWidget(self.lbl_warn)
        body.addWidget(bar_card)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        tcard, tl = card(margins=10)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Student", "Email", "Score", "Review", "Returned"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_STUDENT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_EMAIL, QHeaderView.ResizeMode.Stretch)
        for c in (COL_SCORE, COL_REVIEW, COL_RETURN):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        tl.addWidget(self.table, 1)
        self.lbl_count = label("", "muted")
        tl.addWidget(self.lbl_count)
        split.addWidget(tcard)

        right = QWidget()
        rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(12)

        # preview
        pc, pl = card()
        prow = QHBoxLayout()
        prow.addWidget(label("Email preview", "sectionTitle")); prow.addStretch()
        self.btn_pdf = button("Open PDF", "ghost")
        prow.addWidget(self.btn_pdf)
        pl.addLayout(prow)
        self.lbl_to = label("", "muted"); self.lbl_subject = label("")
        self.lbl_subject.setWordWrap(True)
        f = self.lbl_subject.font(); f.setBold(True); self.lbl_subject.setFont(f)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMinimumHeight(110)
        self.lbl_attach = label("", "muted")
        for w in (self.lbl_to, self.lbl_subject, self.preview, self.lbl_attach):
            pl.addWidget(w)
        rl.addWidget(pc)

        # Canvas: feedback PDF as a submission comment (grades untouched)
        cc, cl = card()
        cl.addWidget(label("Canvas feedback", "sectionTitle"))
        cform = QFormLayout()
        urow = QHBoxLayout()
        self.in_canvas_url = QLineEdit(CANVAS_CONFIG.get("base_url", ""))
        self.in_canvas_url.setPlaceholderText("https://canvas.charlotte.edu")
        self.btn_canvas_connect = button("Connect")
        self.btn_canvas_forget = button("Forget token", "ghost")
        urow.addWidget(self.in_canvas_url, 1); urow.addWidget(self.btn_canvas_connect)
        cform.addRow("Canvas", urow)
        self.cb_course = QComboBox(); self.cb_assignment = QComboBox()
        for cb in (self.cb_course, self.cb_assignment):
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(20)
        cform.addRow("Course", self.cb_course)
        cform.addRow("Assignment", self.cb_assignment)
        self.in_canvas_comment = QLineEdit(CANVAS_CONFIG.get("comment", ""))
        self.in_canvas_comment.setToolTip("Placeholders: {name} {first_name} {exam} {version} {total} {max} {percent}")
        cform.addRow("Comment", self.in_canvas_comment)
        cl.addLayout(cform)
        self.lbl_canvas = label("Attaches each student's feedback PDF as a comment on the assignment "
                                "(like SpeedGrader's paperclip). Grades are not changed.", "muted")
        self.lbl_canvas.setWordWrap(True)
        cl.addWidget(self.lbl_canvas)
        crow = QHBoxLayout()
        self.btn_canvas_sel = button("Upload to selected")
        self.btn_canvas_all = button("Upload to all not yet uploaded", "primary")
        crow.addWidget(self.btn_canvas_forget); crow.addStretch()
        crow.addWidget(self.btn_canvas_sel); crow.addWidget(self.btn_canvas_all)
        cl.addLayout(crow)
        rl.addWidget(cc)

        # email
        ec, el = card()
        el.addWidget(label("Email", "sectionTitle"))
        self.lbl_smtp = label("", "muted"); self.lbl_smtp.setWordWrap(True)
        el.addWidget(self.lbl_smtp)
        r1 = QHBoxLayout()
        self.btn_test = button("Send test to me")
        self.btn_send_sel = button("Send to selected")
        self.btn_send_all = button("Send to all not yet sent", "primary")
        for b in (self.btn_test, self.btn_send_sel, self.btn_send_all):
            r1.addWidget(b)
        el.addLayout(r1)
        r2 = QHBoxLayout()
        self.progress = QProgressBar(); self.progress.setVisible(False)
        self.btn_stop = button("Stop", "danger"); self.btn_stop.setVisible(False)
        r2.addWidget(self.progress, 1); r2.addWidget(self.btn_stop)
        el.addLayout(r2)
        self.lbl_status = label("", "muted"); self.lbl_status.setWordWrap(True)
        el.addWidget(self.lbl_status)
        rl.addWidget(ec)

        # LMS
        lc, ll = card()
        ll.addWidget(label("LMS upload", "sectionTitle"))
        form = QFormLayout()
        self.cb_preset = QComboBox()
        for k, v in PRESETS.items():
            self.cb_preset.addItem(v, k)
        self.in_pattern = QLineEdit(DEFAULT_PATTERN)
        self.in_pattern.setToolTip("Placeholders: {student_id} {first} {last} {name} {exam} {version} {sub_id}")
        form.addRow("Format", self.cb_preset)
        form.addRow("File names", self.in_pattern)
        ll.addLayout(form)
        self.lbl_lms_help = label("", "muted"); self.lbl_lms_help.setWordWrap(True)
        ll.addWidget(self.lbl_lms_help)
        self.btn_lms = button("Export ZIP…", "primary")
        ll.addWidget(self.btn_lms)
        rl.addWidget(lc)
        rl.addStretch()

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(right)
        right.setObjectName("page"); right.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        split.addWidget(scroll)
        split.setSizes([640, 520])
        body.addWidget(split, 1)

        self.cb_exam.currentIndexChanged.connect(self.load)
        self.chk_review.toggled.connect(self.load)
        self.table.itemSelectionChanged.connect(self.show_preview)
        self.btn_pdf.clicked.connect(self.open_pdf)
        self.btn_test.clicked.connect(self.send_test)
        self.btn_send_sel.clicked.connect(lambda: self.send(selected_only=True))
        self.btn_send_all.clicked.connect(lambda: self.send(selected_only=False))
        self.btn_stop.clicked.connect(self.stop)
        self.btn_lms.clicked.connect(self.export_lms)
        self.cb_preset.currentIndexChanged.connect(self._preset_changed)
        self.btn_canvas_connect.clicked.connect(self.canvas_connect)
        self.btn_canvas_forget.clicked.connect(self.canvas_forget)
        self.cb_course.activated.connect(lambda _: self.canvas_load_assignments())
        self.cb_assignment.activated.connect(lambda _: self._canvas_remember())
        self.btn_canvas_sel.clicked.connect(lambda: self.canvas_upload(selected_only=True))
        self.btn_canvas_all.clicked.connect(lambda: self.canvas_upload(selected_only=False))
        self._preset_changed()
        self.refresh()

    # ---------- data ----------
    def refresh(self):
        cur = self.cb_exam.currentText()
        self.cb_exam.blockSignals(True)
        self.cb_exam.clear()
        self.cb_exam.addItems(list_exams())
        if cur:
            i = self.cb_exam.findText(cur)
            if i >= 0:
                self.cb_exam.setCurrentIndex(i)
        self.cb_exam.blockSignals(False)
        s = SMTP_CONFIG
        if s.get("username") or s.get("from_address"):
            self.lbl_smtp.setText(f"From {s.get('from_address') or s.get('username')} via "
                                  f"{s.get('host')}:{s.get('port')} ({s.get('security')}). Change in Settings.")
        else:
            self.lbl_smtp.setText("Email is not set up yet. Fill in “Email (SMTP)” in Settings.")
        self.load()

    def load(self):
        exam = self.cb_exam.currentText()
        all_items = collect_items(exam, include_needs_review=True) if exam else []
        n_review = sum(1 for it in all_items if it.needs_review)
        self.items = [it for it in all_items if self.chk_review.isChecked() or not it.needs_review]
        self.lbl_warn.setText(f"{n_review} test(s) still need review"
                              + ("" if self.chk_review.isChecked() else " — excluded") if n_review else "")
        self.table.setRowCount(0)
        for it in self.items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._fill_row(r, it)
        no_email = sum(1 for it in self.items if not it.email)
        self.lbl_count.setText(f"{len(self.items)} tests" + (f" · {no_email} without email" if no_email else ""))
        if self.items:
            self.table.selectRow(0)
        else:
            self._clear_preview()
        self._canvas_select_target()
        self._update_enabled()

    def _fill_row(self, r, it):
        name = it.student.name if it.student else f"Unassigned (#{it.sub.id})"
        self.table.setItem(r, COL_STUDENT, QTableWidgetItem(name))
        em = QTableWidgetItem(it.email or "no email")
        if not it.email:
            em.setForeground(QColor(theme.WARN)); em.setBackground(QColor(theme.WARN_SOFT))
        self.table.setItem(r, COL_EMAIL, em)
        self.table.setItem(r, COL_SCORE, QTableWidgetItem(f"{it.total:g} / {it.max:g}"))
        rv = QTableWidgetItem("needs review" if it.needs_review else "ok")
        rv.setForeground(QColor(theme.WARN if it.needs_review else theme.OK))
        self.table.setItem(r, COL_REVIEW, rv)
        last = it.last()
        if last is None:
            txt, color = "never", theme.MUTED
        elif last["status"] == "sent":
            how = "Canvas" if last["channel"] == "canvas" else "emailed"
            txt, color = f"{how} {last['sent_at'].replace('T', ' ')[:16]}", theme.OK
        elif last["status"] == "exported":
            txt, color = f"LMS export {last['sent_at'][:10]}", theme.ACCENT
        else:
            txt, color = f"{last['status']}: {last['error'][:40]}", theme.DANGER
        ri = QTableWidgetItem(txt); ri.setForeground(QColor(color))
        if last and last.get("error"):
            ri.setToolTip(last["error"])
        self.table.setItem(r, COL_RETURN, ri)

    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.items[r] for r in rows if r < len(self.items)]

    def _clear_preview(self):
        for w in (self.lbl_to, self.lbl_subject, self.lbl_attach):
            w.setText("")
        self.preview.setPlainText("")

    def show_preview(self):
        sel = self._selected()
        if not sel:
            self._clear_preview(); return
        it = sel[0]
        self.lbl_to.setText(f"To: {it.email or '(no email address)'}")
        self.lbl_subject.setText(render(SMTP_CONFIG.get("subject", ""), it))
        self.preview.setPlainText(render(SMTP_CONFIG.get("body", ""), it))
        from ..returns.items import pdf_name
        self.lbl_attach.setText(f"Attachment: {pdf_name(it)}")
        self._update_enabled()

    def open_pdf(self):
        sel = self._selected()
        if not sel:
            return
        try:
            path = ensure_pdf(sel[0])
        except Exception as e:
            QMessageBox.critical(self, "PDF", str(e)); return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _update_enabled(self):
        busy = self.worker is not None
        has = bool(self.items)
        for b in (self.btn_test, self.btn_send_all, self.btn_lms):
            b.setEnabled(not busy and has)
        self.btn_send_sel.setEnabled(not busy and bool(self._selected()))
        self.btn_pdf.setEnabled(bool(self._selected()))
        target = self.cb_assignment.currentData() is not None
        self.btn_canvas_all.setEnabled(not busy and has and target)
        self.btn_canvas_sel.setEnabled(not busy and target and bool(self._selected()))
        for w in (self.cb_exam, self.chk_review, self.btn_canvas_connect, self.cb_course, self.cb_assignment):
            w.setEnabled(not busy)

    def _preset_changed(self):
        preset = self.cb_preset.currentData()
        self.in_pattern.setEnabled(preset == "generic")
        self.lbl_lms_help.setText({
            "generic": "A ZIP with one PDF per student (named with the pattern) and grades.csv.",
            "canvas": "Import the CSV in Gradebook → Import. Canvas has no bulk feedback-file upload: "
                      "attach each PDF (named by SIS id) in SpeedGrader.",
            "moodle": "Upload the ZIP via Assignment → View all submissions → Upload multiple feedback files "
                      "in a zip. Moodle matches folders by its participant id; the roster's student_id is used, "
                      "so you may need Moodle's grading worksheet to map ids.",
        }[preset])

    # ---------- email ----------
    def _smtp_ready(self) -> bool:
        s = SMTP_CONFIG
        if not s.get("host") or not (s.get("from_address") or s.get("username")):
            QMessageBox.information(self, "Email", "Set up “Email (SMTP)” in Settings first.")
            return False
        return True

    def _password(self):
        user = SMTP_CONFIG.get("username", "")
        if not user:
            return ""
        pw = get_password(user)
        if pw:
            return pw
        d = PasswordDialog(user, self)
        if d.exec() != QDialog.DialogCode.Accepted or not d.pw.text():
            return None
        set_password(user, d.pw.text(), remember=d.remember.isChecked())
        return d.pw.text()

    def send_test(self):
        if not self._smtp_ready() or not self.items:
            return
        sel = self._selected()
        item = sel[0] if sel else self.items[0]
        me = SMTP_CONFIG.get("from_address") or SMTP_CONFIG.get("username")
        pw = self._password()
        if pw is None:
            return
        w = Worker(job_test, dict(SMTP_CONFIG), pw, me, item)
        w.done.connect(lambda to: self.lbl_status.setText(f"Test email sent to {to}. Check your inbox."))
        self._start(w)

    def send(self, selected_only: bool):
        if not self._smtp_ready():
            return
        cands = self._selected() if selected_only else self.items
        todo = [it for it in cands if it.email and (selected_only or not was_emailed(it.sub.id, it.email))]
        no_email = [it for it in cands if not it.email]
        if not todo:
            QMessageBox.information(self, "Email", "Nothing to send." + (
                f" {len(no_email)} selected test(s) have no email address." if no_email else ""))
            return
        already = [it for it in todo if was_emailed(it.sub.id, it.email)]
        names = [f"{it.student.name if it.student else '?'} <{it.email}>" for it in todo]
        listing = "\n".join(names[:15]) + (f"\n… and {len(names) - 15} more" if len(names) > 15 else "")
        msg = f"Send {len(todo)} email(s) with the feedback PDF attached?\n\n{listing}"
        if no_email:
            msg += f"\n\nSkipped (no email): {len(no_email)}"
        if already:
            msg += f"\n\n{len(already)} of these were already sent and will be sent AGAIN."
        if QMessageBox.question(self, "Confirm sending", msg) != QMessageBox.StandardButton.Yes:
            return
        pw = self._password()
        if pw is None:
            return
        w = Worker(job_send_resend if already else job_send, todo, dict(SMTP_CONFIG), pw, False)
        w.item.connect(self._on_result)
        w.done.connect(self._send_done)
        self._start(w)

    def _on_result(self, res):
        for r, it in enumerate(self.items):
            if it.sub.id == res["submission_id"]:
                self._fill_row(r, it)
        if res["status"] == "failed":
            self.lbl_status.setText(f"Failed for {res['recipient']}: {res['error']}")

    def _send_done(self, results):
        sent = sum(1 for r in results if r["status"] == "sent")
        failed = [r for r in results if r["status"] == "failed"]
        skipped = sum(1 for r in results if r["status"] == "skipped")
        txt = f"Sent {sent}" + (f", failed {len(failed)}" if failed else "") + (f", skipped {skipped}" if skipped else "")
        self.lbl_status.setText(txt + ".")
        if failed:
            QMessageBox.warning(self, "Email", txt + "\n\n" + "\n".join(
                f"{r['recipient']}: {r['error']}" for r in failed[:10]))

    # ---------- Canvas ----------
    def _canvas_url(self) -> str:
        url = canvas.normalize_url(self.in_canvas_url.text())
        if url != CANVAS_CONFIG.get("base_url"):
            CANVAS_CONFIG["base_url"] = url
            save_settings()
        return url

    def _canvas_token(self, url):
        tok = canvas.get_token(url)
        if tok:
            return tok
        d = PasswordDialog(url, self, title="Canvas access token", field="Token",
                           prompt=f"Access token for {url}.\nCreate one in Canvas: Account → Settings → "
                                  "“+ New Access Token”.")
        if d.exec() != QDialog.DialogCode.Accepted or not d.pw.text().strip():
            return None
        canvas.set_token(url, d.pw.text().strip(), remember=d.remember.isChecked())
        return d.pw.text().strip()

    def _canvas_failed(self, url, error):
        if "access token" in error:   # 401: ask for a new token next time
            canvas.forget_token(url)

    def canvas_forget(self):
        url = self._canvas_url()
        if url:
            canvas.forget_token(url)
            self.lbl_canvas.setText("Token forgotten. Connect asks for it again.")

    def canvas_connect(self):
        url = self._canvas_url()
        if not url:
            QMessageBox.information(self, "Canvas", "Enter your Canvas address, e.g. https://canvas.charlotte.edu")
            return
        tok = self._canvas_token(url)
        if tok is None:
            return
        w = Worker(job_canvas_courses, url, tok)
        w.done.connect(lambda res: QTimer.singleShot(0, lambda: self._canvas_courses_loaded(*res)))
        w.failed.connect(lambda e: self._canvas_failed(url, e))
        self._start(w)

    def _canvas_courses_loaded(self, me, courses):
        self.cb_course.clear(); self.cb_assignment.clear()
        for c in courses:
            code = c.get("course_code") or ""
            name = c.get("name") or code or str(c["id"])
            self.cb_course.addItem(name if not code or code in name else f"{name} ({code})", c["id"])
        self.lbl_canvas.setText(f"Connected as {me}. {len(courses)} course(s)." if me else
                                f"Connected. {len(courses)} course(s).")
        if not courses:
            self._update_enabled(); return
        t = CANVAS_CONFIG.get("targets", {}).get(self.cb_exam.currentText())
        i = self.cb_course.findData(t["course_id"]) if t else -1
        self.cb_course.setCurrentIndex(max(0, i))
        self.canvas_load_assignments()

    def canvas_load_assignments(self):
        cid = self.cb_course.currentData()
        url = self._canvas_url()
        tok = canvas.get_token(url)
        if cid is None or not tok:
            return
        w = Worker(job_canvas_assignments, url, tok, cid)
        w.done.connect(lambda res: QTimer.singleShot(0, lambda: self._canvas_assignments_loaded(*res)))
        w.failed.connect(lambda e: self._canvas_failed(url, e))
        self._start(w)

    def _canvas_assignments_loaded(self, course_id, assignments):
        if course_id != self.cb_course.currentData():
            return
        self.cb_assignment.clear()
        self.cb_assignment.addItem("— choose the assignment —", None)
        for a in assignments:
            self.cb_assignment.addItem(a["name"] + ("" if a.get("published", True) else " (unpublished)"), a["id"])
        self._canvas_select_target()

    def _canvas_select_target(self):
        """Pick the assignment remembered for this exam, else one named like the exam."""
        if not hasattr(self, "cb_assignment") or self.cb_assignment.count() == 0:
            return
        exam = self.cb_exam.currentText()
        t = CANVAS_CONFIG.get("targets", {}).get(exam)
        if t and t["course_id"] != self.cb_course.currentData():
            i = self.cb_course.findData(t["course_id"])
            if i >= 0:
                self.cb_course.setCurrentIndex(i)
                self.canvas_load_assignments()
                return
        i = self.cb_assignment.findData(t["assignment_id"]) if t else -1
        if i < 0 and exam:
            i = next((k for k in range(1, self.cb_assignment.count())
                      if self.cb_assignment.itemText(k).strip().lower() == exam.strip().lower()), 0)
        self.cb_assignment.setCurrentIndex(max(0, i))
        self._update_enabled()

    def _canvas_remember(self):
        cid, aid, exam = self.cb_course.currentData(), self.cb_assignment.currentData(), self.cb_exam.currentText()
        if cid is not None and aid is not None and exam:
            CANVAS_CONFIG.setdefault("targets", {})[exam] = {"course_id": cid, "assignment_id": aid}
            save_settings()
        self._update_enabled()

    def canvas_upload(self, selected_only: bool):
        cid, aid = self.cb_course.currentData(), self.cb_assignment.currentData()
        cands = self._selected() if selected_only else list(self.items)
        if cid is None or aid is None or not cands:
            return
        url = self._canvas_url()
        tok = self._canvas_token(url)
        if tok is None:
            return
        self._canvas_remember()
        CANVAS_CONFIG["comment"] = self.in_canvas_comment.text()
        save_settings()
        w = Worker(job_canvas_match, url, tok, cid, cands)
        w.done.connect(lambda ms: QTimer.singleShot(0, lambda: self._canvas_confirm(url, tok, cid, aid, ms,
                                                                                    selected_only)))
        w.failed.connect(lambda e: self._canvas_failed(url, e))
        self._start(w)

    def _canvas_confirm(self, url, tok, cid, aid, matches, selected_only):
        key = canvas.target_key(cid, aid)
        unmatched = [m for m in matches if m.user is None]
        todo = [m for m in matches if m.user is not None
                and (selected_only or not was_returned(m.item.sub.id, "canvas", key))]
        if not todo:
            QMessageBox.information(self, "Canvas", "Nothing to upload." + (
                f" {len(unmatched)} test(s) could not be matched to a Canvas student." if unmatched else ""))
            return
        already = [m for m in todo if was_returned(m.item.sub.id, "canvas", key)]

        def who(m):
            mine = m.item.student.name if m.item.student else f"#{m.item.sub.id}"
            theirs = m.user["name"] if m.user else ""
            return (f"{mine} → {theirs}" + ("" if m.how == "student id" else f"  ({m.how})")) if m.user \
                else f"{mine}: {m.how}"
        listing = "\n".join(who(m) for m in todo[:15]) + (f"\n… and {len(todo) - 15} more" if len(todo) > 15 else "")
        msg = (f"Upload {len(todo)} feedback PDF(s) to\n{self.cb_course.currentText()} → "
               f"{self.cb_assignment.currentText()}?\n\n{listing}")
        if unmatched:
            msg += (f"\n\nNot matched, skipped ({len(unmatched)}):\n" + "\n".join(who(m) for m in unmatched[:10])
                    + "\nFix their student ID or email in Students to match Canvas.")
        if already:
            msg += f"\n\n{len(already)} of these were already uploaded and will be uploaded AGAIN."
        if QMessageBox.question(self, "Confirm upload", msg) != QMessageBox.StandardButton.Yes:
            return
        w = Worker(job_canvas_upload, url, tok, cid, aid, self.in_canvas_comment.text(), todo, bool(already))
        w.item.connect(self._on_result)
        w.done.connect(self._canvas_done)
        w.failed.connect(lambda e: self._canvas_failed(url, e))
        self._start(w)

    def _canvas_done(self, results):
        sent = sum(1 for r in results if r["status"] == "sent")
        failed = [r for r in results if r["status"] == "failed"]
        txt = f"Uploaded {sent} to Canvas" + (f", failed {len(failed)}" if failed else "") + "."
        self.lbl_status.setText(txt)
        if any("access token" in r["error"] for r in failed):
            canvas.forget_token(self._canvas_url())
        if failed:
            QMessageBox.warning(self, "Canvas", txt + "\n\n" + "\n".join(
                f"{r['recipient']}: {r['error']}" for r in failed[:10]))

    # ---------- LMS ----------
    def export_lms(self):
        if not self.items:
            return
        out = QFileDialog.getExistingDirectory(self, "Export to folder")
        if not out:
            return
        preset = self.cb_preset.currentData()
        w = Worker(job_lms, self.cb_exam.currentText(), out, preset, self.in_pattern.text().strip() or DEFAULT_PATTERN,
                   list(self.items))
        w.done.connect(lambda res: self._lms_done(res, out))
        self._start(w)

    def _lms_done(self, res, out):
        self.lbl_status.setText(f"Exported {len(res['files'])} PDFs to {os.path.basename(res['zip'])}.")
        for r in range(len(self.items)):
            self._fill_row(r, self.items[r])
        note = "\n\n".join(res["warnings"])
        if QMessageBox.question(self, "Exported", f"Wrote:\n{res['zip']}\n{res['csv']}"
                                + (f"\n\n{note}" if note else "") + "\n\nOpen the folder?") == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))

    # ---------- worker plumbing ----------
    def _start(self, w):
        self.worker = w
        w.progress.connect(self._on_progress)
        w.done.connect(lambda _: self._finished(None))
        w.failed.connect(self._finished)
        self.progress.setVisible(True); self.btn_stop.setVisible(True)
        self._update_enabled()
        w.start()

    def _on_progress(self, i, n, msg):
        self.progress.setRange(0, max(1, n)); self.progress.setValue(i)
        self.lbl_status.setText(msg)

    def stop(self):
        if self.worker:
            self.worker.cancelled = True
            self.lbl_status.setText("Stopping after the current message…")

    def _finished(self, error):
        self.worker = None
        self.progress.setVisible(False); self.btn_stop.setVisible(False)
        if error:
            self.lbl_status.setText(f"Error: {error}")
            QMessageBox.critical(self, "Error", error)
        self._update_enabled()

    def shutdown(self):
        if self.worker:
            self.worker.cancelled = True
            self.worker.wait(30000)

def job_send_resend(items, settings, password, dry_run, worker=None):
    return send_batch(items, settings, password, progress=worker.progress.emit,
                      should_stop=lambda: worker.cancelled, dry_run=dry_run, resend=True, on_result=worker.item.emit)
