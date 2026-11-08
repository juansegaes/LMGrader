"""Per-problem grading time limit and the Names dropdown filter (offline, no network)."""
import threading
import time
from types import SimpleNamespace as NS

from lmgrader.llm import client as lc
from lmgrader.llm.client import LLMClient
from lmgrader.models import SUB_ERROR


def _client(create):
    c = LLMClient()
    c._client = NS(chat=NS(completions=NS(create=create)), close=lambda: None)
    return c


def test_hung_request_goes_to_human_review(monkeypatch):
    monkeypatch.setattr(lc, "GRADE_TIMEOUT_S", 0.3)
    release = threading.Event()   # ignores its HTTP timeout, like a server sending keep-alive bytes
    c = _client(lambda **kw: release.wait(10))
    t0 = time.monotonic()
    res = c.grade_answer("b64", "1", "g", "rubric", 4)
    release.set()
    assert time.monotonic() - t0 < 2
    assert res["needs_human_review"] and res["review_reason"] == "llm_error" and res["score"] == 0


def test_failing_server_stops_at_the_limit(monkeypatch):
    monkeypatch.setattr(lc, "GRADE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(lc, "RETRY_FAILURES", 99)
    monkeypatch.setattr(lc, "RETRY_WAIT_S", 0.05)
    def down(**kw):
        raise ConnectionError("down")
    t0 = time.monotonic()
    assert _client(down).grade_answer("b64", "1", "g", "rubric", 4)["review_reason"] == "llm_error"
    assert time.monotonic() - t0 < 2


def test_stop_ends_a_hung_request_quickly(monkeypatch):
    release = threading.Event()
    c = _client(lambda **kw: release.wait(10))
    threading.Timer(0.2, c.abort).start()
    t0 = time.monotonic()
    assert c.grade_answer("b64", "1", "g", "rubric", 4)["review_reason"] == "llm_error"
    release.set()
    assert time.monotonic() - t0 < 2


def test_fast_reply_is_unaffected_and_budget_is_cleared():
    reply = NS(choices=[NS(message=NS(content='{"score": 3, "feedback": "ok", "needs_human_review": false, '
                                              '"review_reason": "", "transcription": "x", "confidence": 0.9, '
                                              '"continues_elsewhere": false}'))])
    c = _client(lambda **kw: reply)
    res = c.grade_answer("b64", "1", "g", "rubric", 4)
    assert res["score"] == 3 and not res["needs_human_review"]
    assert c._deadline is None


def test_dropdown_only_offers_unassigned_students():
    from lmgrader.ui.scan_view import unassigned_students
    students = [NS(id=i) for i in (1, 2, 3, 4)]
    a, b = NS(student_id=1, status="ready"), NS(student_id=None, status="identified")
    failed = NS(student_id=3, status=SUB_ERROR)      # a test in error doesn't hold its student
    subs = [a, b, failed]
    assert [s.id for s in unassigned_students(students, subs, a)] == [1, 2, 3, 4]
    assert [s.id for s in unassigned_students(students, subs, b)] == [2, 3, 4]
