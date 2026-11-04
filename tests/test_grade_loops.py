"""A grading reply where the model loops until max_tokens: retried once, then salvaged and flagged."""
import json
from lmgrader.llm.client import LLMClient, LOOP_RETRY_NOTE, run_grading

LOOP = ("The student's calculation of det(A) is not shown, so the 2 pt criterion is not met. "
        "The student's calculation of det(A_1(\\vec b)) and det(A_2(\\vec b)) is correct, and the solution is correct. ")
# the real reply that prompted this: valid start, then the feedback repeats until it is cut off
LOOPING = ('{\n  "transcription": "\\\\[ \\\\det(A) = 2 \\\\]\\n\\\\[ x_1 = 4 \\\\quad x_2 = -\\\\frac{5}{2} \\\\]",\n'
           '  "score": 5,\n  "feedback": "The student correctly found x_1 = 4 and x_2 = -5/2. '
           + json.dumps(LOOP * 30)[1:-1] + 'The student\'s calculation of det(A_1(\\\\vec b)) and')
GOOD = json.dumps({"transcription": "x_1 = 4, x_2 = -5/2", "score": 6, "feedback": "Correct.",
                   "needs_human_review": False, "review_reason": "", "confidence": 0.9,
                   "continues_elsewhere": False})

def _client(replies):
    c = LLMClient(base_url="http://127.0.0.1:9/v1", api_key="x")
    sent = []
    def chat(messages, schema=None, schema_name="response", **kw):
        sent.append(messages[-1]["content"][0]["text"])
        return replies[len(sent) - 1]
    c._chat = chat
    return c, sent

def _msgs():
    return [{"role": "user", "content": [{"type": "text", "text": "grade this"}]}]

def test_loop_is_retried_with_a_strict_note():
    c, sent = _client([LOOPING, GOOD])
    res = run_grading(c, _msgs(), None, 6)
    assert len(sent) == 2 and sent[1].endswith(LOOP_RETRY_NOTE) and not sent[0].endswith(LOOP_RETRY_NOTE)
    assert res["score"] == 6 and res["feedback"] == "Correct." and not res["needs_human_review"]

def test_still_looping_is_salvaged_and_flagged():
    c, sent = _client([LOOPING, LOOPING])
    res = run_grading(c, _msgs(), None, 6)
    assert len(sent) == 2
    assert res["score"] == 5                                  # the AI's suggestion survives
    assert res["needs_human_review"] and "truncated_response" in res["review_reason"]
    assert res["transcription"].startswith(r"\[ \det(A) = 2 \]")
    assert res["feedback"].startswith("The student correctly found x_1 = 4")
    assert res["feedback"].count("is not shown") == 1          # one copy of the loop, not thirty
    assert len(res["feedback"]) < 400 and res["confidence"] == 0.0

def test_complete_json_with_repeated_feedback_is_cut():
    looped = json.dumps({"transcription": "x", "score": 2, "feedback": "Good start. " + LOOP * 5,
                         "needs_human_review": False, "review_reason": "", "confidence": 0.9,
                         "continues_elsewhere": False})
    c, sent = _client([looped, looped])
    res = run_grading(c, _msgs(), None, 4)
    assert res["score"] == 2 and res["feedback"].count("is not shown") == 1
    assert res["needs_human_review"] and "repetitive_output" in res["review_reason"]

def test_normal_reply_is_not_retried():
    c, sent = _client([GOOD])
    assert run_grading(c, _msgs(), None, 6)["score"] == 6 and len(sent) == 1
