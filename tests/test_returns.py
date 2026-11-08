"""Returning tests: email (fake SMTP, no network) and LMS exports."""
import csv
import json
import smtplib
import zipfile
import pytest
from lmgrader import db, config
from lmgrader.grading import session, Grader
from lmgrader.models import Student
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from lmgrader.returns import collect_items, build_message, send_batch, send_test, set_password, export_lms, export_pdfs
from lmgrader.returns import mailer
from conftest import FakeLLM, make_page, SCREENSHOT_DIR

class FakeSMTP:
    """Captures messages; recipients in `refuse` are rejected."""
    sent, logins, instances = [], [], 0
    refuse = set()

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.instances += 1
    def ehlo(self): pass
    def starttls(self, context=None): pass
    def login(self, u, p): FakeSMTP.logins.append((u, p))
    def send_message(self, msg):
        if msg["To"] in FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        FakeSMTP.sent.append(msg)
    def quit(self): pass
    def close(self): pass

@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.instances, FakeSMTP.refuse = [], [], 0, set()
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP

SETTINGS = {"host": "smtp.example.com", "port": 465, "security": "SSL", "username": "teacher@example.com",
            "from_name": "Teacher", "from_address": "teacher@example.com", "delay": 0,
            "subject": "{exam}: {total}/{max}", "body": "Hi {first_name}, you got {percent}% on {exam} v{version}."}

def make_exam(tmp, name, students):
    """Single-page exam with one 4-pt question; one graded submission per (student name, email)."""
    p = str(tmp / f"{name}.png")
    save_image(p, make_page(31, name.upper()))
    t = create_template_from_image(f"{name} vA", p, name, "A")
    r = add_region(t, "Q1", "g", (100, 300, 300, 150), 4, rubric="- [4 pt] both roots")
    r.question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    pages = []
    for i in range(len(students)):
        sp = str(tmp / f"{name}_s{i}.png"); save_image(sp, make_page(31, name.upper())); pages.append(sp)
    subs, _ = session.create_submissions(name, pages, 1, source=f"{name}.pdf")
    for sub, (sname, email, sid) in zip(subs, students):
        stid = db.add_student(Student(name=sname, email=email, student_id=sid))
        sub.template_id = t.id
        session.confirm_student(sub, stid)
        session.grade_one(Grader(FakeLLM()), sub)
    return subs

ROSTER = [("Ana Torres", "ana@example.com", "S001"), ("Smith, John", "john@example.com", "S002"),
          ("No Mail", "", "S003")]

def test_build_message_attachment_and_placeholders(tmp_path):
    make_exam(tmp_path, "RetMsg", ROSTER[:1])
    item = collect_items("RetMsg")[0]
    from lmgrader.returns import ensure_pdf
    pdf = ensure_pdf(item)
    msg = build_message(item, settings=SETTINGS, pdf_path=pdf)
    assert msg["To"] == "ana@example.com"
    assert msg["Subject"] == "RetMsg: 3/4"
    body = msg.get_body(("plain",)).get_content()
    assert "Hi Ana, you got 75% on RetMsg vA." in body
    att = list(msg.iter_attachments())
    assert len(att) == 1 and att[0].get_content_type() == "application/pdf"
    assert att[0].get_filename().endswith(".pdf") and att[0].get_content()[:4] == b"%PDF"
    # (sub, student, pdf, settings) call form
    msg2 = build_message(item.sub, item.student, pdf, SETTINGS)
    assert msg2["Subject"] == "RetMsg: 3/4"

def test_batch_sends_once_then_skips(tmp_path):
    make_exam(tmp_path, "RetBatch", ROSTER)
    items = collect_items("RetBatch")
    res = send_batch(items, SETTINGS, "pw")
    by = {r["recipient"]: r["status"] for r in res}
    assert by == {"ana@example.com": "sent", "john@example.com": "sent", "": "skipped"}
    assert len(FakeSMTP.sent) == 2 and FakeSMTP.instances == 1          # one connection
    assert FakeSMTP.logins == [("teacher@example.com", "pw")]
    assert FakeSMTP.sent[1]["Subject"] == "RetBatch: 3/4"
    assert "Hi John," in FakeSMTP.sent[1].get_body(("plain",)).get_content()   # "Smith, John" -> John
    # second run (e.g. after a crash): nothing is sent twice
    res2 = send_batch(collect_items("RetBatch"), SETTINGS, "pw")
    assert len(FakeSMTP.sent) == 2
    assert {r["status"] for r in res2} == {"skipped"}
    sent_rows = [r for r in db.list_returns() if r["channel"] == "email" and r["status"] == "sent"
                 and r["submission_id"] in {i.sub.id for i in items}]
    assert len(sent_rows) == 2
    # explicit resend
    send_batch(items[:1], SETTINGS, "pw", resend=True)
    assert len(FakeSMTP.sent) == 3

def test_failure_recorded_and_others_continue(tmp_path):
    make_exam(tmp_path, "RetFail", ROSTER[:2])
    FakeSMTP.refuse = {"ana@example.com"}
    items = collect_items("RetFail")
    res = send_batch(items, SETTINGS, "pw")
    st = {r["recipient"]: r for r in res}
    assert st["ana@example.com"]["status"] == "failed" and "no such user" in st["ana@example.com"]["error"]
    assert st["john@example.com"]["status"] == "sent"
    ana = next(i for i in items if i.email == "ana@example.com")
    assert db.last_return(ana.sub.id, "email")["status"] == "failed"
    assert not db.was_emailed(ana.sub.id, "ana@example.com")
    # fixed later: a re-run sends only the failed one
    FakeSMTP.refuse = set()
    send_batch(collect_items("RetFail"), SETTINGS, "pw")
    assert [m["To"] for m in FakeSMTP.sent] == ["john@example.com", "ana@example.com"]

def test_auth_failure_stops_batch(tmp_path, monkeypatch):
    make_exam(tmp_path, "RetAuth", ROSTER[:2])
    def bad_login(self, u, p):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
    monkeypatch.setattr(FakeSMTP, "login", bad_login)
    res = send_batch(collect_items("RetAuth"), SETTINGS, "wrong")
    assert len(res) == 1 and res[0]["status"] == "failed" and "authentication" in res[0]["error"]
    assert FakeSMTP.instances == 1   # no retry storm

def test_dry_run_sends_nothing(tmp_path):
    make_exam(tmp_path, "RetDry", ROSTER[:2])
    items = collect_items("RetDry")
    res = send_batch(items, SETTINGS, "pw", dry_run=True)
    assert {r["status"] for r in res} == {"dry_run"}
    assert FakeSMTP.sent == [] and FakeSMTP.instances == 0
    assert not any(db.was_emailed(i.sub.id, i.email) for i in items)

def test_send_test_goes_to_teacher(tmp_path):
    make_exam(tmp_path, "RetTest", ROSTER[:1])
    item = collect_items("RetTest")[0]
    send_test(SETTINGS, "pw", "teacher@example.com", item)
    assert len(FakeSMTP.sent) == 1
    m = FakeSMTP.sent[0]
    assert m["To"] == "teacher@example.com" and m["Subject"].startswith("[TEST] ")
    assert db.last_return(item.sub.id, "email") is None   # tests are not recorded as returns

def test_password_never_written_to_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(mailer, "_keyring", None)
    set_password("teacher@example.com", "s3cret-app-pw", remember=True)
    config.SMTP_CONFIG["password"] = "s3cret-app-pw"      # even if something sneaks it in
    config.SMTP_CONFIG["username"] = "teacher@example.com"
    try:
        config.save_settings()
        text = (tmp_path / "settings.json").read_text(encoding="utf-8")
        assert "s3cret-app-pw" not in text
        data = json.loads(text)
        assert data["smtp"]["username"] == "teacher@example.com" and "password" not in data["smtp"]
        assert mailer.get_password("teacher@example.com") == "s3cret-app-pw"   # session memory
    finally:
        config.SMTP_CONFIG.pop("password", None)
        config.SMTP_CONFIG["username"] = ""

def test_settings_legacy_flat_file_still_loads(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"base_url": "http://x:1/v1", "model": "m1"}), encoding="utf-8")
    monkeypatch.setattr(config, "settings_path", lambda: p)
    old = dict(config.LLM_CONFIG)
    try:
        config.load_settings()
        assert config.LLM_CONFIG["model"] == "m1"
    finally:
        config.LLM_CONFIG.clear(); config.LLM_CONFIG.update(old)

def _csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))

def test_lms_generic(tmp_path):
    make_exam(tmp_path, "RetLms", ROSTER)
    out = tmp_path / "out"
    res = export_lms("RetLms", str(out), "generic")
    with zipfile.ZipFile(res["zip"]) as z:
        names = sorted(z.namelist())
    assert names == ["S001_Torres_Ana.pdf", "S002_Smith_John.pdf", "S003_Mail_No.pdf"]
    rows = _csv(res["csv"])
    assert rows[0] == ["name", "student_id", "email", "total", "max", "percent", "pdf"]
    assert ["Ana Torres", "S001", "ana@example.com", "3", "4", "75", "S001_Torres_Ana.pdf"] in rows
    assert all(r["status"] == "exported" for r in db.list_returns() if r["channel"] == "lms")
    # custom pattern
    res2 = export_lms("RetLms", str(tmp_path / "out2"), "generic", pattern="{exam}-{last}")
    with zipfile.ZipFile(res2["zip"]) as z:
        assert "RetLms-Torres.pdf" in z.namelist()

def test_lms_canvas(tmp_path):
    make_exam(tmp_path, "RetCanvas", ROSTER[:2])
    res = export_lms("RetCanvas", str(tmp_path / "c"), "canvas")
    rows = _csv(res["csv"])
    assert rows[0] == ["Student", "ID", "SIS User ID", "Section", "RetCanvas (points)"]
    assert rows[1][0] == "Points Possible" and rows[1][4] == "4"
    assert ["Torres, Ana", "", "S001", "", "3"] in rows
    assert ["Smith, John", "", "S002", "", "3"] in rows
    with zipfile.ZipFile(res["zip"]) as z:
        assert sorted(z.namelist()) == ["S001.pdf", "S002.pdf"]
    assert any("SpeedGrader" in w for w in res["warnings"])

def test_lms_moodle(tmp_path):
    make_exam(tmp_path, "RetMoodle", ROSTER[:2])
    res = export_lms("RetMoodle", str(tmp_path / "m"), "moodle")
    with zipfile.ZipFile(res["zip"]) as z:
        names = sorted(z.namelist())
    assert names == ["Ana Torres_S001_assignsubmission_file_/RetMoodle_feedback.pdf",
                     "John Smith_S002_assignsubmission_file_/RetMoodle_feedback.pdf"]
    rows = _csv(res["csv"])
    assert rows[0][:5] == ["Identifier", "Full name", "Email address", "Grade", "Maximum Grade"]
    assert rows[1][0] == "Participant S001"
    assert any("participant id" in w for w in res["warnings"])

def test_return_view_smoke(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ENABLED", True)
    subs = make_exam(tmp_path, "RetUI", ROSTER)
    send_batch(collect_items("RetUI")[:1], SETTINGS, "pw")   # one already emailed
    from lmgrader.ui.return_view import ReturnView
    config.SMTP_CONFIG.update(username="teacher@example.com", from_address="teacher@example.com")
    try:
        v = ReturnView()
        v.resize(1300, 820)
        v.show()
        v.cb_exam.setCurrentIndex(v.cb_exam.findText("RetUI"))
        qapp.processEvents()
        assert v.table.rowCount() == 3
        texts = [v.table.item(r, 4).text() for r in range(3)]
        assert sum(t.startswith("emailed") for t in texts) == 1 and texts.count("never") == 2
        assert v.table.item(2, 1).text() in ("no email", "ana@example.com", "john@example.com")
        v.table.selectRow(0); qapp.processEvents()
        assert v.lbl_to.text().startswith("To: ") and "RetUI" in v.lbl_subject.text()
        assert "teacher@example.com" in v.lbl_smtp.text()
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        v.grab().save(str(SCREENSHOT_DIR / "return.png"))
    finally:
        config.SMTP_CONFIG.update(username="", from_address="")

def test_return_view_without_email(qapp, tmp_path):
    make_exam(tmp_path, "RetNoMail", ROSTER)
    assert config.EMAIL_ENABLED is False                      # the shipped default
    from lmgrader.ui.return_view import ReturnView
    v = ReturnView()
    v.resize(1300, 820)
    v.show()
    v.cb_exam.setCurrentIndex(v.cb_exam.findText("RetNoMail"))
    qapp.processEvents()
    assert v.email_card.isHidden()
    assert v.table.isColumnHidden(1)
    assert v.lbl_preview_title.text() == "Feedback PDF"
    assert v.btn_pdfs.isEnabled() and v.btn_grades_csv.isEnabled()
    v.table.selectRow(0); qapp.processEvents()
    assert v.lbl_attach.text().startswith("File: ")
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    v.grab().save(str(SCREENSHOT_DIR / "return_no_email.png"))

def test_export_pdfs(tmp_path):
    make_exam(tmp_path, "RetPdfs", ROSTER)
    out = tmp_path / "pdfs"
    files = export_pdfs(collect_items("RetPdfs"), str(out), "{last}_{first}")
    assert sorted(files) == ["Mail_No.pdf", "Smith_John.pdf", "Torres_Ana.pdf"]
    assert all((out / f).is_file() for f in files)
    rows = [r for r in db.list_returns() if r["channel"] == "pdf"]
    assert len(rows) == 3 and all(r["status"] == "exported" for r in rows)
