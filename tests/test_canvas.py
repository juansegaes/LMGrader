"""Canvas feedback upload against a fake Canvas (no network)."""
import json
import urllib.parse
import pytest
from lmgrader import db, config
from lmgrader.returns import collect_items, canvas
from lmgrader.returns.canvas import CanvasClient, CanvasAuthError, match_students, upload_batch, normalize_url
from test_returns import make_exam

COURSE, ASSIGN = 77, 501
USERS = [
    {"id": 1, "name": "Ana Torres", "sortable_name": "Torres, Ana", "sis_user_id": "S001",
     "login_id": "atorres", "email": "ana@example.com"},
    {"id": 2, "name": "John Smith", "sortable_name": "Smith, John", "sis_user_id": None,
     "login_id": "jsmith", "email": "john@example.com"},
    {"id": 3, "name": "Maria José Pérez", "sortable_name": "Pérez, Maria José", "sis_user_id": "S900",
     "login_id": "mperez", "email": "mp@example.com"},
]

class FakeCanvas:
    """Just enough Canvas: paged users (2 per page), comment-file uploads via a 3xx confirm,
    submission comments."""
    def __init__(self, token="good", confirm="redirect"):
        self.token, self.confirm = token, confirm
        self.calls, self.comments, self.files = [], [], {}
        self.fail_user = None

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((method, u.path, headers))
        if u.netloc == "files.example":                      # file store: must not get the token
            assert "Authorization" not in headers
            assert b"%PDF" in body and b'name="key"' in body
            fid = len(self.files) + 1000
            self.files[fid] = body
            if self.confirm == "redirect":
                return 302, {"location": f"https://canvas.example/api/v1/files/{fid}/create_success"}, b""
            return 201, {}, json.dumps({"id": fid}).encode()
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {}, json.dumps({"errors": [{"message": "Invalid access token."}]}).encode()
        p = u.path
        if p == "/api/v1/users/self":
            return 200, {}, b'{"id": 9, "name": "Prof"}'
        if p == f"/api/v1/courses/{COURSE}/users":
            assert q["enrollment_type[]"] == ["student"]
            page = int(q.get("page", ["1"])[0])
            chunk = USERS[(page - 1) * 2: page * 2]
            link = (f'<https://canvas.example/api/v1/courses/{COURSE}/users?enrollment_type[]=student'
                    f'&page={page + 1}&per_page=2>; rel="next"') if page * 2 < len(USERS) else ""
            return 200, {"link": link}, json.dumps(chunk).encode()
        if p.endswith("/comments/files") and method == "POST":
            return 200, {}, json.dumps({"upload_url": "https://files.example/up",
                                        "upload_params": {"key": "abc"}, "file_param": "file"}).encode()
        if p.startswith("/api/v1/files/") and p.endswith("/create_success"):
            return 200, {}, json.dumps({"id": int(p.split("/")[4])}).encode()
        if method == "PUT" and "/submissions/" in p:
            user = int(p.rsplit("/", 1)[1])
            if user == self.fail_user:
                return 500, {}, b'{"errors": [{"message": "boom"}]}'
            form = urllib.parse.parse_qs(body.decode(), keep_blank_values=True)
            assert "submission[posted_grade]" not in form          # grades are never touched
            self.comments.append((user, form["comment[text_comment]"][0], form["comment[file_ids][]"]))
            return 200, {}, b'{"id": 1}'
        return 404, {}, b'{"errors": [{"message": "not found"}]}'

ROSTER = [("Ana Torres", "", "S001"),                 # by SIS id
          ("Smith, John", "john@example.com", ""),    # by email
          ("Maria Jose Perez", "", ""),               # by name (accents differ)
          ("Nobody Here", "", "S404")]                # not in the course

def test_normalize_url():
    assert normalize_url("canvas.charlotte.edu/") == "https://canvas.charlotte.edu"
    assert normalize_url("https://canvas.charlotte.edu/api/v1") == "https://canvas.charlotte.edu"

def test_match_students(tmp_path):
    make_exam(tmp_path, "CvMatch", ROSTER)
    ms = {m.item.student.name: m for m in match_students(collect_items("CvMatch"), USERS)}
    assert ms["Ana Torres"].user["id"] == 1 and ms["Ana Torres"].how == "student id"
    assert ms["Smith, John"].user["id"] == 2 and ms["Smith, John"].how == "email"
    assert ms["Maria Jose Perez"].user["id"] == 3
    assert ms["Nobody Here"].user is None and "not found" in ms["Nobody Here"].how

def test_two_tests_on_one_student_are_held(tmp_path):
    make_exam(tmp_path, "CvDup", [("Ana Torres", "", "S001"), ("Ana Torres", "ana@example.com", "")])
    ms = match_students(collect_items("CvDup"), USERS)
    assert all(m.user is None and "same Canvas student" in m.how for m in ms)

@pytest.mark.parametrize("confirm", ["redirect", "created"])
def test_upload_once_then_skip(tmp_path, confirm, monkeypatch):
    monkeypatch.setattr(canvas.time, "sleep", lambda s: None)
    make_exam(tmp_path, f"CvUp{confirm}", ROSTER)
    fake = FakeCanvas(confirm=confirm)
    client = CanvasClient("canvas.example", "good", transport=fake)
    ms = match_students(collect_items(f"CvUp{confirm}"), client.list_students(COURSE))   # paged: 2 + 1
    res = upload_batch(ms, client, COURSE, ASSIGN, "Your {exam}: {total}/{max}")
    st = {r["recipient"]: r["status"] for r in res}
    assert st == {"Ana Torres": "sent", "John Smith": "sent", "Maria José Pérez": "sent", "Nobody Here": "skipped"}
    assert sorted(c[0] for c in fake.comments) == [1, 2, 3]
    assert all(c[1] == f"Your CvUp{confirm}: 3/4" and len(c[2]) == 1 for c in fake.comments)
    assert len(fake.files) == 3
    # run again (e.g. after a crash): nothing uploaded twice
    res2 = upload_batch(match_students(collect_items(f"CvUp{confirm}"), USERS), client, COURSE, ASSIGN, "x")
    assert len(fake.comments) == 3 and {r["status"] for r in res2} == {"skipped"}
    # another assignment is a different target
    upload_batch(ms[:1], client, COURSE, ASSIGN + 1, "x")
    assert len(fake.comments) == 4

def test_failure_logged_others_continue(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas.time, "sleep", lambda s: None)
    make_exam(tmp_path, "CvFail", ROSTER[:2])
    fake = FakeCanvas(); fake.fail_user = 1
    client = CanvasClient("https://canvas.example", "good", transport=fake)
    res = upload_batch(match_students(collect_items("CvFail"), USERS), client, COURSE, ASSIGN)
    st = {r["recipient"]: r for r in res}
    assert st["Ana Torres"]["status"] == "failed" and "boom" in st["Ana Torres"]["error"]
    assert st["John Smith"]["status"] == "sent"
    fake.fail_user = None
    upload_batch(match_students(collect_items("CvFail"), USERS), client, COURSE, ASSIGN)
    assert [c[0] for c in fake.comments] == [2, 1]          # only the failed one is retried

def test_bad_token_stops_batch(tmp_path):
    make_exam(tmp_path, "CvAuth", ROSTER[:2])
    fake = FakeCanvas(token="good")
    client = CanvasClient("https://canvas.example", "wrong", transport=fake)
    with pytest.raises(CanvasAuthError):
        client.whoami()
    res = upload_batch(match_students(collect_items("CvAuth"), USERS), client, COURSE, ASSIGN)
    assert len(res) == 1 and res[0]["status"] == "failed" and "access token" in res[0]["error"]

def test_token_never_written_to_settings(tmp_path, monkeypatch):
    from lmgrader.returns import mailer
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(mailer, "_keyring", None)
    canvas.set_token("canvas.example", "tok-s3cret")
    config.CANVAS_CONFIG["token"] = "tok-s3cret"            # even if something sneaks it in
    try:
        config.save_settings()
        text = (tmp_path / "settings.json").read_text(encoding="utf-8")
        assert "tok-s3cret" not in text and "canvas" in json.loads(text)
        assert canvas.get_token("https://canvas.example/") == "tok-s3cret"   # session memory
        canvas.forget_token("canvas.example")
        assert canvas.get_token("canvas.example") is None
    finally:
        config.CANVAS_CONFIG.pop("token", None)

def test_return_view_has_canvas_card(qapp, tmp_path):
    make_exam(tmp_path, "CvUI", ROSTER[:1])
    from lmgrader.ui.return_view import ReturnView
    v = ReturnView()
    v.cb_exam.setCurrentIndex(v.cb_exam.findText("CvUI"))
    qapp.processEvents()
    assert not v.btn_canvas_all.isEnabled()                 # no assignment chosen yet
    v._canvas_courses_loaded("Prof", [{"id": COURSE, "name": "MATH 1241", "course_code": "MATH-1241-001"}])
    v.worker = None
    v._canvas_assignments_loaded(COURSE, [{"id": ASSIGN, "name": "CvUI", "published": True}])
    assert v.cb_assignment.currentData() == ASSIGN          # picked by name
    v._update_enabled()
    assert v.btn_canvas_all.isEnabled()
