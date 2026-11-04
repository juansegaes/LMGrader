"""LLM requests are retried until they fail 3 times within a minute; Stop ends the retries."""
import pytest
from lmgrader.llm import client as cl
from lmgrader.llm.client import LLMClient

@pytest.fixture
def clock(monkeypatch):
    """Fake time: waiting between attempts advances the clock instead of sleeping."""
    now = [0.0]
    monkeypatch.setattr(cl.time, "monotonic", lambda: now[0])
    return now

def _client(clock, outcomes, wait=cl.RETRY_WAIT_S, call_s=0.0):
    """outcomes: per attempt, an Exception to raise or a str to return; each attempt takes call_s."""
    c = LLMClient(base_url="http://127.0.0.1:9/v1", api_key="x")
    calls = []
    def once(*a, **k):
        calls.append(clock[0])
        clock[0] += call_s
        o = outcomes[len(calls) - 1]
        if isinstance(o, Exception):
            raise o
        return o
    c._chat_once = once
    def fake_wait(t):
        clock[0] += wait
        return c._aborted.is_set()
    c._aborted.wait = fake_wait
    return c, calls

def test_recovers_after_transient_errors(clock):
    c, calls = _client(clock, [RuntimeError("busy"), RuntimeError("busy"), "ok"])
    assert c._chat([]) == "ok" and len(calls) == 3

def test_gives_up_after_three_failures_within_a_minute(clock):
    c, calls = _client(clock, [RuntimeError(f"down {i}") for i in range(10)])
    with pytest.raises(RuntimeError, match="down 2"):
        c._chat([])
    assert len(calls) == 3

def test_slow_failures_spread_over_more_than_a_minute_keep_retrying(clock):
    # each failure takes 40 s: never 3 inside one minute until the server answers
    errs = [TimeoutError("slow")] * 5
    c, calls = _client(clock, errs + ["ok"], call_s=40.0)
    assert c._chat([]) == "ok" and len(calls) == 6

def test_stop_ends_retries(clock):
    c, calls = _client(clock, [RuntimeError("x")] * 10)
    c.abort()
    with pytest.raises(RuntimeError):
        c._chat([])
    assert len(calls) == 1

def test_grade_answer_errors_only_after_retries(clock):
    c, calls = _client(clock, [RuntimeError("down")] * 10)
    res = c.grade_answer("aGk=", "1", "g", "- [2 pt] x", 2)
    assert res["review_reason"] == "llm_error" and len(calls) == 3
