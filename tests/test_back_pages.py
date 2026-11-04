"""Back pages: blank / minor / work detection, LLM routing of back work, grading with continuation."""
import json
import os
from pathlib import Path
import cv2
import numpy as np
import pytest

from lmgrader import db
from lmgrader.grading import Grader, session
from lmgrader.grading.back_page import analyze_back_image
from lmgrader.llm.back_pages import resolve_labels, route_back_page, bbox_to_pixels
from lmgrader.llm.client import LLMClient
from lmgrader.models import Student, SUB_GRADED
from lmgrader.template.alignment import save_image
from lmgrader.template.template import create_template_from_image, add_region, persist
from conftest import FakeLLM, make_page

CLASS_PDF = Path(__file__).parent / "fixtures" / "private" / "class_scans.pdf"
# 1-indexed PDF pages (backs are the even pages) — verified by eye
TRUTH_WORK = {26, 28, 44, 50, 82, 84, 90, 98, 100, 104, 118}
TRUTH_MINOR = {16, 48}   # a name in the corner; an 'ad-bc' scribble

def _blank_page(w=850, h=1100):
    return np.full((h, w, 3), 255, np.uint8)

def _scribble(img, x, y, lines=1, words=4, seed=0):
    """Handwriting-ish strokes: random polylines grouped in words and lines."""
    rng = np.random.default_rng(seed)
    for li in range(lines):
        cx = x
        for _ in range(words):
            pts = [(cx + k * 6, y + li * 40 + int(rng.integers(-8, 8))) for k in range(8)]
            cv2.polylines(img, [np.array(pts, np.int32)], False, (40, 40, 40), 2)
            cx += 70
    return img

# ---------- detection (synthetic) ----------
def test_edge_line_and_speckle_is_blank():
    img = _blank_page()
    cv2.line(img, (845, 0), (845, 1099), (0, 0, 0), 2)          # scanner edge line
    cv2.line(img, (3, 100), (3, 600), (30, 30, 30), 1)
    rng = np.random.default_rng(1)
    for _ in range(80):                                           # dust
        x, y = rng.integers(40, 800), rng.integers(40, 1050)
        img[y, x] = 0
    for k in range(6):                                            # bleed-through bubbles
        cv2.circle(img, (330, 120 + 70 * k), 4, (90, 90, 90), -1)
    assert analyze_back_image(img)["level"] == "blank"

def test_name_in_corner_is_minor():
    img = _scribble(_blank_page(), 680, 1000, lines=1, words=2)
    r = analyze_back_image(img)
    assert r["level"] == "minor" and not r["flagged"] and r["blobs"]

def test_block_of_work_is_work_and_resolution_independent():
    img = _scribble(_blank_page(), 80, 100, lines=8, words=6)
    r = analyze_back_image(img)
    assert r["level"] == "work" and r["flagged"]
    big = cv2.resize(img, (2550, 3300), interpolation=cv2.INTER_NEAREST)     # 300 dpi
    big = cv2.erode(big, np.ones((3, 3), np.uint8))                          # strokes scale too
    assert analyze_back_image(big)["level"] == "work"

# ---------- detection (real class scans) ----------
@pytest.mark.skipif(not CLASS_PDF.exists(), reason="private class scans not available")
def test_real_backs_confusion():
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(CLASS_PDF))
    missed, false_pos, wrong_minor = [], [], []
    for i in range(1, len(pdf), 2):
        p = i + 1
        img = np.array(pdf[i].render(scale=100 / 72).to_pil().convert("RGB"))[:, :, ::-1]
        level = analyze_back_image(img)["level"]
        if p in TRUTH_WORK and level != "work":
            missed.append((p, level))
        if p not in TRUTH_WORK and level == "work":
            false_pos.append(p)
        if p in TRUTH_MINOR and level != "minor":
            wrong_minor.append((p, level))
    pdf.close()
    assert not missed, missed
    assert len(false_pos) <= 2, false_pos
    assert not wrong_minor, wrong_minor

# ---------- routing helpers ----------
def test_resolve_labels():
    labels = ["1a", "1b", "2a", "2b", "5", "Q6(a)"]
    assert resolve_labels("2a)", labels) == ["2a"]
    assert resolve_labels("1)", labels) == ["1a", "1b"]          # bare number = all parts
    assert resolve_labels("5.", labels) == ["5"]
    assert resolve_labels("6a", labels) == ["Q6(a)"]
    assert resolve_labels("", labels) == [] and resolve_labels("9z", labels) == []

def test_bbox_to_pixels():
    assert bbox_to_pixels(None, 100, 200) == (0, 0, 100, 200)
    x, y, w, h = bbox_to_pixels([100, 100, 500, 500], 1000, 1000, pad=0)
    assert (x, y, w, h) == (100, 100, 400, 400)

class RouteLLM(FakeLLM):
    """Fake client exposing _chat/_parse like LLMClient. Records the grading calls."""
    def __init__(self, route):
        super().__init__("")
        self.route, self.calls = route, []

    def _chat(self, messages, schema=None, schema_name="response"):
        if schema_name == "back_page":
            return json.dumps(self.route)
        if schema_name == "grade":
            n_img = sum(1 for p in messages[-1]["content"] if p.get("type") == "image_url")
            text = messages[-1]["content"][0]["text"]
            self.calls.append((text.split(" ")[1], n_img))
            return json.dumps({"transcription": "x=2 [back] x=-2", "score": 4, "feedback": "complete",
                               "needs_human_review": False, "review_reason": "", "confidence": 0.95,
                               "continues_elsewhere": False})
        raise AssertionError(schema_name)

    def _parse(self, text, max_points):
        return LLMClient._parse(None, text, max_points)

    def grade_answer(self, b, problem, *a, **kw):
        self.calls.append((problem, 1))
        res = super().grade_answer(b, problem, *a, **kw)
        res.update(confidence=0.95, continues_elsewhere=False)
        return res

def test_route_back_page_expands_and_flags_unknown():
    llm = RouteLLM({"items": [{"problem": "1)", "box": [0, 0, 500, 300], "summary": "T(1,0)"},
                              {"problem": "", "box": [0, 400, 900, 700], "summary": "matrix"},
                              {"problem": "2a", "box": [], "summary": "rref"}], "unlabeled": False})
    r = route_back_page(llm, "x", ["1a", "1b", "2a"])
    probs = [it["problem"] for it in r["items"]]
    assert probs == ["1a", "1b", None, "2a"] and r["unlabeled"]
    assert r["items"][-1]["bbox"] is None

# ---------- grading integration ----------
def _two_region_exam(tmp, name):
    p1 = str(tmp / "t1.png"); save_image(p1, make_page(71, "BACKS EXAM"))
    t = create_template_from_image(f"{name} vA", [p1], name, "A")
    for i, lab in enumerate(("1a", "2a", "2b")):
        r = add_region(t, lab, "g", (80, 200 + 230 * i, 300, 150), 4, rubric="- [4 pt] both roots")
        r.question_text = r"Solve $x^2-4=0$."
    t.id = persist(t)
    front = str(tmp / "front.png"); save_image(front, make_page(71, "BACKS EXAM"))
    back = str(tmp / "back.png"); save_image(back, _scribble(_blank_page(), 80, 100, lines=8, words=6))
    subs, _ = session.create_submissions(name, [front, back], 1, duplex=True, source="b.pdf")
    sub = subs[0]
    sub.template_id = t.id
    session.confirm_student(sub, db.add_student(Student(name=f"{name} Student")))
    return sub

def test_grader_routes_back_work_to_its_problem(tmp_path, monkeypatch):
    from lmgrader.config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", False)   # the fake gives full marks
    sub = _two_region_exam(tmp_path, "BacksExam")
    assert sub.backs and sub.backs[0]
    llm = RouteLLM({"items": [{"problem": "2a", "box": [50, 50, 700, 450], "summary": "rest of 2a"}],
                    "unlabeled": False})
    session.grade_one(Grader(llm), sub)
    assert sorted(llm.calls) == [("1a", 1), ("2a", 2), ("2b", 1)]     # only 2a got the back page
    g = {x.problem: x for x in db.list_grades(sub.id)}
    assert g["2a"].needs_human_review and "work_on_back" in g["2a"].review_reason
    assert "[back]" in g["2a"].transcription
    assert "work_on_back" not in g["1a"].review_reason and "work_on_back" not in g["2b"].review_reason
    s = db.get_submission(sub.id)
    assert s.status == SUB_GRADED and s.has_work_on_back

def test_full_marks_with_back_work_needs_no_human(tmp_path, monkeypatch):
    """Same routing, default settings: 2a (graded with its back page) got full marks -> no flag."""
    from lmgrader.config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "full_marks_ignore_back", True)
    sub = _two_region_exam(tmp_path, "BacksExamFull")
    llm = RouteLLM({"items": [{"problem": "2a", "box": [50, 50, 700, 450], "summary": "rest of 2a"}],
                    "unlabeled": False})
    session.grade_one(Grader(llm), sub)
    g = {x.problem: x for x in db.list_grades(sub.id)}
    assert g["2a"].score == 4 and "[back]" in g["2a"].transcription     # still graded with the back
    assert not any("work_on_back" in x.review_reason for x in g.values())
    assert db.get_submission(sub.id).back_reviewed

def test_unlabeled_back_work_flags_the_page(tmp_path):
    sub = _two_region_exam(tmp_path, "BacksExam2")
    llm = RouteLLM({"items": [{"problem": "", "box": [50, 50, 700, 450], "summary": "?"}], "unlabeled": True})
    session.grade_one(Grader(llm), sub)
    assert all(n == 1 for _, n in llm.calls)
    for g in db.list_grades(sub.id):
        assert "work_on_back (unlabeled)" in g.review_reason

def test_routing_failure_falls_back_to_flagging(tmp_path):
    sub = _two_region_exam(tmp_path, "BacksExam3")
    session.grade_one(Grader(FakeLLM("")), sub)    # FakeLLM has no _chat -> routing fails
    for g in db.list_grades(sub.id):
        assert "work_on_back" in g.review_reason

# ---------- live (LM Studio) ----------
def _lm_studio_up():
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:1234/v1/models", timeout=2)
        return True
    except Exception:
        return False

EXAM_LABELS = ["1a", "1b", "2a", "2b", "3a", "3b", "4a", "4b", "4c", "5", "6a", "6b",
               "7a", "7b", "7c", "8a", "8b", "9a", "9b", "9c", "9d", "9e", "9f"]
LIVE_TRUTH = {26: {"1a", "1b", "3a", "3b", "5"}, 50: {"2a"}, 82: {"2a", "2b"}, 104: {"8a"}}

@pytest.mark.skipif(not (os.environ.get("LMGRADER_LIVE") and CLASS_PDF.exists() and _lm_studio_up()),
                    reason="set LMGRADER_LIVE=1 with LM Studio running")
def test_live_route_real_backs():
    import pypdfium2 as pdfium
    from lmgrader.llm.client import image_to_b64, downscale_for_llm
    llm = LLMClient(model=os.environ.get("LMGRADER_TEST_MODEL", "qwen3-vl-8b-instruct"), max_tokens=2048)
    pdf = pdfium.PdfDocument(str(CLASS_PDF))
    hits = 0
    for p, truth in LIVE_TRUTH.items():
        img = np.array(pdf[p - 1].render(scale=150 / 72).to_pil().convert("RGB"))[:, :, ::-1]
        r = route_back_page(llm, image_to_b64(downscale_for_llm(img)[0]), EXAM_LABELS)
        got = {it["problem"] for it in r["items"] if it["problem"]}
        print(f"page {p}: truth={sorted(truth)} got={sorted(got)} unlabeled={r['unlabeled']}")
        hits += bool(got & truth)
    pdf.close()
    assert hits >= 3
