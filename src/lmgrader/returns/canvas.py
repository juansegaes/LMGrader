"""Return feedback PDFs through the Canvas REST API (stdlib only).

Each student's feedback PDF is attached as a submission comment on a Canvas assignment
(what SpeedGrader's paperclip does). Grades are never touched.

  1. POST .../submissions/:user/comments/files  -> upload_url + upload_params
  2. POST the file (multipart) to upload_url    -> 201 with the file, or 3xx to confirm (GET with auth)
  3. PUT  .../submissions/:user  comment[text_comment], comment[file_ids][]

Safety: like email, every attempt is logged in `returns` (channel 'canvas', recipient
'course/assignment'), and a submission already uploaded to that assignment is skipped unless
resend=True. The token is never written to settings.json (keyring or session memory only).
"""
import json
import mimetypes
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
from rapidfuzz import fuzz, process
from ..db import add_return, was_returned
from .items import ReturnItem, ensure_pdf
from .mailer import render, get_password, set_password, forget_password

KEYRING_SERVICE = "LMGrader-Canvas"
NAME_ACCEPT = 92      # fuzzy name fallback: WRatio needed...
NAME_MARGIN = 8       # ...and lead over the runner-up

class CanvasError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Canvas {status}: {message}" if status else message)
        self.status = status

class CanvasAuthError(CanvasError):
    pass

# ---- token storage (keyed by the Canvas host) ----
def get_token(base_url: str) -> Optional[str]:
    return get_password(normalize_url(base_url), KEYRING_SERVICE)

def set_token(base_url: str, token: str, remember: bool = True):
    set_password(normalize_url(base_url), token, remember=remember, service=KEYRING_SERVICE)

def forget_token(base_url: str):
    forget_password(normalize_url(base_url), KEYRING_SERVICE)

def normalize_url(url: str) -> str:
    """'canvas.charlotte.edu/', 'https://canvas.charlotte.edu/api/v1' -> 'https://canvas.charlotte.edu'."""
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return re.sub(r"/api/v1$", "", url)

# ---- HTTP ----
Response = Tuple[int, Dict[str, str], bytes]

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None    # 3xx come back as responses; the client decides (upload confirm needs auth)

_opener = urllib.request.build_opener(_NoRedirect)

def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Response:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""

def _multipart(fields: Dict[str, str], file_field: str, filename: str, data: bytes,
               ctype: str) -> Tuple[bytes, str]:
    boundary = "----LMGrader" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
               f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
    out.append(data)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"

def _next_link(link: str) -> Optional[str]:
    for part in (link or "").split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None

class CanvasClient:
    def __init__(self, base_url: str, token: str, transport: Callable[..., Response] = urllib_transport):
        self.base = normalize_url(base_url)
        self.token = token
        self.transport = transport

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}/api/v1{path}"

    def _call(self, method: str, path: str, params=None, form=None, auth: bool = True) -> Response:
        url = self._url(path)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, hdrs, data = self.transport(method, url, headers, body)
        _raise_for(status, data)
        return status, hdrs, data

    def _json(self, method: str, path: str, params=None, form=None):
        _, _, data = self._call(method, path, params, form)
        return json.loads(data or b"null")

    def _paged(self, path: str, params=None) -> list:
        out, url, params = [], path, dict(params or {}, per_page=100)
        while url:
            _, hdrs, data = self._call("GET", url, params)
            out.extend(json.loads(data or b"[]"))
            url, params = _next_link(hdrs.get("link", "")), None   # next link already has the query
        return out

    # ---- reads ----
    def whoami(self) -> dict:
        return self._json("GET", "/users/self")

    def list_courses(self) -> List[dict]:
        """Active courses where the user teaches (teacher or TA)."""
        cs = self._paged("/courses", {"enrollment_state": "active"})
        teach = [c for c in cs if any(e.get("type") in ("teacher", "ta") for e in c.get("enrollments") or [])]
        return sorted(teach or cs, key=lambda c: c.get("name") or "")

    def list_assignments(self, course_id) -> List[dict]:
        return self._paged(f"/courses/{course_id}/assignments", {"order_by": "position"})

    def list_students(self, course_id) -> List[dict]:
        return self._paged(f"/courses/{course_id}/users",
                           {"enrollment_type[]": "student", "include[]": "email"})

    # ---- write ----
    def upload_comment_file(self, course_id, assignment_id, user_id, path: str) -> int:
        name = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/pdf"
        pre = self._json("POST", f"/courses/{course_id}/assignments/{assignment_id}/submissions/"
                                 f"{user_id}/comments/files",
                         form={"name": name, "size": len(data), "content_type": ctype})
        body, mp = _multipart(pre.get("upload_params") or {}, pre.get("file_param") or "file", name, data, ctype)
        # the upload URL may be another host (file storage): no token there
        status, hdrs, resp = self.transport("POST", pre["upload_url"], {"Content-Type": mp}, body)
        if hdrs.get("location") and (300 <= status < 400 or (status == 201 and not _has_id(resp))):
            _, _, resp = self._call("GET", hdrs["location"])        # confirm the upload
        else:
            _raise_for(status, resp)
        info = json.loads(resp or b"{}")
        if "id" not in info:
            raise CanvasError(status, f"upload of {name} did not return a file id")
        return int(info["id"])

    def comment(self, course_id, assignment_id, user_id, text: str, file_ids: List[int]) -> dict:
        form = [("comment[text_comment]", text)] + [("comment[file_ids][]", str(i)) for i in file_ids]
        return self._json("PUT", f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}",
                          form=form)

def _has_id(resp: bytes) -> bool:
    try:
        return "id" in json.loads(resp or b"{}")
    except ValueError:
        return False

def _raise_for(status: int, data: bytes):
    if 200 <= status < 400:
        return
    try:
        j = json.loads(data or b"{}")
        errs = j.get("errors")
        msg = (errs[0].get("message") if isinstance(errs, list) and errs and isinstance(errs[0], dict)
               else json.dumps(errs) if errs else j.get("message")) or ""
    except (ValueError, AttributeError):
        msg = (data or b"").decode("utf-8", "replace")[:200]
    msg = msg or f"HTTP {status}"
    if status == 401:
        raise CanvasAuthError(status, msg + " (the access token is invalid or expired)")
    if status == 403:
        raise CanvasError(status, msg + " (no permission: are you a teacher in this course?)")
    raise CanvasError(status, msg)

# ---- matching LMGrader students to Canvas users ----
def _norm(s: str) -> str:
    """Lowercase, accents and punctuation dropped: 'Pérez, María-José' -> 'perez maria jose'."""
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()

def _name_keys(u: dict) -> List[str]:
    keys = [_norm(u.get("name")), _norm(u.get("short_name"))]
    sortable = u.get("sortable_name") or ""
    if "," in sortable:
        last, first = sortable.split(",", 1)
        keys.append(_norm(f"{first} {last}"))
    return [k for k in keys if k]

@dataclass
class Match:
    item: ReturnItem
    user: Optional[dict]
    how: str              # 'student id' | 'email' | 'name' | 'similar name' | reason it failed

def match_students(items: List[ReturnItem], users: List[dict]) -> List[Match]:
    """Pair each item with a Canvas user: student id (SIS id / login), then email, then exact name,
    then a clearly winning fuzzy name. Ambiguous or missing -> user None."""
    def index(keyf):
        idx = {}
        for u in users:
            for k in keyf(u):
                if k:
                    idx.setdefault(k, []).append(u)
        return idx
    by_id = index(lambda u: [str(u.get("sis_user_id") or "").strip().lower(),
                             str(u.get("login_id") or "").strip().lower()])
    by_mail = index(lambda u: [str(u.get("email") or "").strip().lower(),
                               str(u.get("login_id") or "").strip().lower()])
    by_name = index(_name_keys)
    choices = {i: max(_name_keys(u), key=len) for i, u in enumerate(users) if _name_keys(u)}
    out = []
    for it in items:
        st = it.student
        if st is None:
            out.append(Match(it, None, "no student assigned")); continue
        sid, mail = (st.student_id or "").strip().lower(), (st.email or "").strip().lower()
        found = None
        for key, idx, how in ((sid, by_id, "student id"), (mail, by_mail, "email"),
                              (mail.split("@")[0] if "@" in mail else "", by_id, "email"),
                              (_norm(st.name), by_name, "name")):
            if key and len(idx.get(key, [])) == 1:
                found = (idx[key][0], how); break
        if found is None and choices and _norm(st.name):
            best = process.extract(_norm(st.name), choices, scorer=fuzz.WRatio, limit=2)
            if best and best[0][1] >= NAME_ACCEPT and (len(best) < 2 or best[0][1] - best[1][1] >= NAME_MARGIN):
                found = (users[best[0][2]], "similar name")
        out.append(Match(it, found[0], found[1]) if found else Match(it, None, "not found in the course"))
    # two tests on one Canvas student is almost surely a wrong match: hold both
    seen = {}
    for m in out:
        if m.user is not None:
            seen.setdefault(m.user["id"], []).append(m)
    for ms in seen.values():
        if len(ms) > 1:
            for m in ms:
                m.how = f"same Canvas student as {len(ms) - 1} other test(s)"
                m.user = None
    return out

def target_key(course_id, assignment_id) -> str:
    return f"{course_id}/{assignment_id}"

def upload_batch(matches: List[Match], client: CanvasClient, course_id, assignment_id, comment: str = "",
                 progress: Callable[[int, int, str], None] = None, should_stop: Callable[[], bool] = None,
                 resend: bool = False, on_result: Callable[[dict], None] = None,
                 make_pdf: Callable[[ReturnItem], str] = ensure_pdf) -> List[dict]:
    """Attach each matched item's feedback PDF as a submission comment. Returns one result dict per
    match: {submission_id, recipient (Canvas name), status: sent|failed|skipped, error}."""
    key = target_key(course_id, assignment_id)
    results, n = [], len(matches)
    for i, m in enumerate(matches):
        if should_stop and should_stop():
            break
        it = m.item
        who = m.user.get("name") if m.user else (it.student.name if it.student else f"submission {it.sub.id}")
        if progress:
            progress(i, n, f"Uploading feedback for {who}…")
        res = {"submission_id": it.sub.id, "recipient": who, "status": "", "error": ""}
        if m.user is None:
            res.update(status="skipped", error=m.how)
        elif not resend and was_returned(it.sub.id, "canvas", key):
            res.update(status="skipped", error="already uploaded")
        else:
            try:
                pdf = make_pdf(it)
                fid = client.upload_comment_file(course_id, assignment_id, m.user["id"], pdf)
                client.comment(course_id, assignment_id, m.user["id"], render(comment, it), [fid])
                res["status"] = "sent"
                add_return(it.sub.id, "canvas", key, "sent", "", pdf)
            except CanvasAuthError as e:
                res.update(status="failed", error=str(e))
                add_return(it.sub.id, "canvas", key, "failed", res["error"], it.pdf_path)
                results.append(res)
                if on_result:
                    on_result(res)
                break    # every following upload would fail the same way
            except Exception as e:
                res.update(status="failed", error=str(e) or e.__class__.__name__)
                add_return(it.sub.id, "canvas", key, "failed", res["error"], it.pdf_path)
        results.append(res)
        if on_result:
            on_result(res)
        if res["status"] == "sent":
            time.sleep(0.2)   # be gentle with Canvas' rate limit
    if progress:
        progress(n, n, "Done")
    return results
