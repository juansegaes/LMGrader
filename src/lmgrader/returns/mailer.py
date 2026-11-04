"""Email graded tests to students over SMTP (stdlib only).

Safety properties:
  * every attempt is recorded in the `returns` table immediately (crash-safe);
  * a submission already sent successfully to an address is skipped unless resend=True,
    so re-running "send to all" after a crash or a partial failure never double-sends;
  * the SMTP password is never written to settings.json (OS keyring or session memory only).
"""
import mimetypes
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Callable, List, Optional
from ..db import add_return, was_emailed
from .items import ReturnItem, ensure_pdf, split_name

KEYRING_SERVICE = "LMGrader-SMTP"
_session_passwords = {}

try:
    import keyring as _keyring
except Exception:   # optional dependency
    _keyring = None

def keyring_available() -> bool:
    return _keyring is not None

def get_password(username: str, service: str = KEYRING_SERVICE) -> Optional[str]:
    if (service, username) in _session_passwords:
        return _session_passwords[(service, username)]
    if _keyring is not None and username:
        try:
            return _keyring.get_password(service, username)
        except Exception:
            return None
    return None

def set_password(username: str, password: str, remember: bool = True, service: str = KEYRING_SERVICE):
    """Keep the password for this session; with remember=True also store it in the OS keyring."""
    _session_passwords[(service, username)] = password
    if remember and _keyring is not None and username:
        try:
            _keyring.set_password(service, username, password)
        except Exception:
            pass

def forget_password(username: str, service: str = KEYRING_SERVICE):
    _session_passwords.pop((service, username), None)
    if _keyring is not None and username:
        try:
            _keyring.delete_password(service, username)
        except Exception:
            pass

def _fmt(v: float) -> str:
    return f"{v:g}"

class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

def placeholders(item: ReturnItem) -> dict:
    name = item.student.name if item.student else (item.sub.name_read or "")
    first, last = split_name(name)
    return {"name": name, "first_name": first or name, "last_name": last, "exam": item.exam,
            "version": item.version, "total": _fmt(item.total), "max": _fmt(item.max),
            "percent": _fmt(item.percent)}

def render(template: str, item: ReturnItem) -> str:
    return (template or "").format_map(_SafeDict(placeholders(item)))

def build_message(sub_or_item, student=None, pdf_path: str = None, settings: dict = None,
                  to_addr: str = None, subject_prefix: str = "") -> EmailMessage:
    """Build the email with the feedback PDF attached. Accepts a ReturnItem (preferred) or the
    (sub, student, pdf_path, settings) form."""
    if isinstance(sub_or_item, ReturnItem):
        item = sub_or_item
        pdf_path = pdf_path or item.pdf_path
    else:
        from ..db import list_grades, load_template
        sub = sub_or_item
        grades = list_grades(sub.id)
        t = load_template(sub.template_id) if sub.template_id else None
        item = ReturnItem(sub=sub, student=student, exam=sub.exam_name or (t.exam_name if t else ""),
                          version=t.version_label if t else "", total=sum(g.score for g in grades),
                          max=sum(g.max_points for g in grades), needs_review=False, pdf_path=pdf_path or "")
    settings = settings or {}
    msg = EmailMessage()
    from_addr = settings.get("from_address") or settings.get("username", "")
    msg["From"] = formataddr((settings.get("from_name", ""), from_addr)) if settings.get("from_name") else from_addr
    msg["To"] = to_addr or item.email
    msg["Subject"] = subject_prefix + render(settings.get("subject", "{exam}"), item)
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1] if "@" in from_addr else None))
    msg.set_content(render(settings.get("body", ""), item))
    if pdf_path:
        with open(pdf_path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(pdf_path)[0] or "application/pdf"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(pdf_path))
    return msg

def _connect(settings: dict, password: str):
    host, port = settings.get("host", ""), int(settings.get("port", 465))
    sec = (settings.get("security") or "SSL").upper()
    if sec == "SSL":
        conn = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        conn.ehlo()
        if sec == "STARTTLS":
            conn.starttls(context=ssl.create_default_context())
            conn.ehlo()
    if settings.get("username"):
        conn.login(settings["username"], password or "")
    return conn

def _close(conn):
    if conn is None:
        return
    try:
        conn.quit()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

def send_batch(items: List[ReturnItem], settings: dict, password: str,
               progress: Callable[[int, int, str], None] = None, should_stop: Callable[[], bool] = None,
               dry_run: bool = False, resend: bool = False, on_result: Callable[[dict], None] = None,
               make_pdf: Callable[[ReturnItem], str] = ensure_pdf) -> List[dict]:
    """Send each item over one SMTP connection (reconnecting if it drops). Returns one result
    dict per item: {submission_id, recipient, status: sent|failed|skipped|dry_run, error}."""
    results, conn = [], None
    n = len(items)
    try:
        for i, item in enumerate(items):
            if should_stop and should_stop():
                break
            to = item.email
            who = item.student.name if item.student else f"submission {item.sub.id}"
            if progress:
                progress(i, n, f"Emailing {who}…")
            res = {"submission_id": item.sub.id, "recipient": to, "status": "", "error": ""}
            if not to:
                res.update(status="skipped", error="no email address")
            elif not resend and was_emailed(item.sub.id, to):
                res.update(status="skipped", error="already sent")
            else:
                try:
                    pdf = make_pdf(item)
                    msg = build_message(item, settings=settings, pdf_path=pdf)
                    if dry_run:
                        res["status"] = "dry_run"
                    else:
                        for attempt in (1, 2):
                            try:
                                if conn is None:
                                    conn = _connect(settings, password)
                                conn.send_message(msg)
                                break
                            except smtplib.SMTPServerDisconnected:
                                _close(conn); conn = None    # dropped connection: reconnect once
                                if attempt == 2:
                                    raise
                            except smtplib.SMTPException:
                                raise                        # auth/recipient/data errors: no retry
                            except OSError:
                                _close(conn); conn = None    # network error: reconnect once
                                if attempt == 2:
                                    raise
                        res["status"] = "sent"
                        add_return(item.sub.id, "email", to, "sent", "", pdf)
                except smtplib.SMTPAuthenticationError as e:
                    res.update(status="failed", error=f"authentication failed: {e.smtp_error!r}")
                    add_return(item.sub.id, "email", to, "failed", res["error"], item.pdf_path)
                    results.append(res)
                    if on_result:
                        on_result(res)
                    break   # every following message would fail the same way
                except Exception as e:
                    res.update(status="failed", error=str(e) or e.__class__.__name__)
                    add_return(item.sub.id, "email", to, "failed", res["error"], item.pdf_path)
            results.append(res)
            if on_result:
                on_result(res)
            if res["status"] == "sent" and i < n - 1:
                time.sleep(float(settings.get("delay", 0) or 0))
    finally:
        _close(conn)
    if progress:
        progress(n, n, "Done")
    return results

def send_test(settings: dict, password: str, to_addr: str, sample_item: ReturnItem,
              make_pdf: Callable[[ReturnItem], str] = ensure_pdf) -> None:
    """Send one sample message (subject prefixed [TEST]) to the teacher's own address.
    Not recorded as a return."""
    pdf = make_pdf(sample_item)
    msg = build_message(sample_item, settings=settings, pdf_path=pdf, to_addr=to_addr, subject_prefix="[TEST] ")
    conn = _connect(settings, password)
    try:
        conn.send_message(msg)
    finally:
        _close(conn)
