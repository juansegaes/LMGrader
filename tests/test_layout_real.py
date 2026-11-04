"""Layout detection + alignment on the real exam (tests/fixtures/test2a.pdf) and real student
scans (tests/fixtures/private/class_scans.pdf, skipped when absent: it is private data).

The live LLM test runs only with LMGRADER_LIVE=1 and LM Studio reachable (≈3 min)."""
import os
from pathlib import Path

import numpy as np
import cv2
import pytest

from lmgrader.template.layout import (find_drawn_boxes, iou, parse_label, order_questions,
                                      refine_layout, page_columns)
from lmgrader.template.alignment import align_with_score
from lmgrader.config import MIN_ALIGN_INLIERS

FIX = Path(__file__).parent / "fixtures"
TEMPLATE_PDF = FIX / "test2a.pdf"
SCANS_PDF = FIX / "private" / "class_scans.pdf"

# drawn rectangles per template page (answer boxes + name/ID boxes on page 1)
EXPECTED_BOXES = {1: 6, 2: 8, 3: 3, 4: 2}
EXPECTED_REGIONS = {1: ["1a", "1b", "2a", "2b"], 2: ["3a", "3b", "4a", "4b", "4c", "5", "6a", "6b"],
                    3: ["7a", "7b", "7c"], 4: ["8a", "8b", "9a", "9b", "9c", "9d", "9e", "9f"]}


def _render(pdf_path, index, scale=300 / 72):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        img = pdf[index].render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


@pytest.fixture(scope="module")
def template_pages():
    if not TEMPLATE_PDF.exists():
        pytest.skip("template fixture missing")
    return {p: _render(TEMPLATE_PDF, p - 1) for p in range(1, 5)}


# ---------- labels / ordering ----------
def test_parse_and_order_labels():
    assert parse_label("1a") == (1, "a") and parse_label("1 (b)") == (1, "b")
    assert parse_label("Q2.c") == (2, "c") and parse_label("10") == (10, "")
    assert parse_label("(c)") is None and parse_label("Name") is None
    W = 2550
    mk = lambda lab, x, y: {"problem": lab, "question_rect": None, "answer_rect": (x, y, 900, 300)}
    # two columns, labels given out of order -> natural label order
    qs = [mk("2a", 1460, 1500), mk("1b", 280, 2300), mk("1a", 280, 1400), mk("2b", 1460, 2700)]
    assert [q["problem"] for q in order_questions(qs, W, 2)] == ["1a", "1b", "2a", "2b"]
    # parts given as bare letters -> parent number attached in column-major reading order
    qs = [mk("(b)", 1460, 2300), mk("7", 280, 700), mk("b", 1460, 200)]
    qs[0]["problem"], qs[1]["problem"], qs[2]["problem"] = "c", "7a", "(b)"
    assert [q["problem"] for q in order_questions(qs, W, 2)] == ["7a", "7b", "7c"]
    # unparseable labels -> column by column, top to bottom
    qs = [mk("x", 1460, 100), mk("y", 280, 900), mk("z", 280, 100)]
    assert [q["problem"] for q in order_questions(qs, W, 2)] == ["z", "y", "x"]
    assert len(page_columns(W, [q["answer_rect"] for q in qs])) == 2


# ---------- rectangle detection on the template ----------
def test_drawn_boxes_on_template(template_pages):
    for p, img in template_pages.items():
        boxes = find_drawn_boxes(img)
        assert len(boxes) == EXPECTED_BOXES[p], (p, boxes)
        for i, a in enumerate(boxes):          # no duplicates (inner/outer edge of the same line)
            assert all(iou(a, b) < 0.8 for b in boxes[i + 1:])


def test_refine_snaps_rough_llm_boxes(template_pages):
    """Simulate a sloppy model answer for page 1 (boxes shifted/shrunk, labels as bare parts,
    no ID box) and check everything snaps to the printed rectangles."""
    img = template_pages[1]
    boxes = sorted(find_drawn_boxes(img), key=lambda b: (b[1], b[0]))
    name_b, id_b = boxes[0], boxes[1]
    answers = [b for b in boxes if b not in (name_b, id_b)]
    jitter = lambda b: (b[0] + 40, b[1] + 30, b[2] - 120, b[3] - 90)
    left = sorted([b for b in answers if b[0] < 1000], key=lambda b: b[1])
    right = sorted([b for b in answers if b[0] >= 1000], key=lambda b: b[1])
    layout = {"questions": [
        {"problem": "1a", "question_rect": None, "answer_rect": jitter(left[0]), "points": None},
        {"problem": "(b)", "question_rect": None, "answer_rect": jitter(left[1]), "points": None},
        {"problem": "2a", "question_rect": None, "answer_rect": jitter(right[0]), "points": None},
        # the model forgot 2b
    ], "name_rect": (name_b[0], name_b[1] - 60, 300, 50), "id_rect": None, "columns": 2}
    out = refine_layout(img, layout)
    got = {q["problem"]: q for q in out["questions"]}
    assert got["1a"]["answer_rect"] == left[0] and got["1b"]["answer_rect"] == left[1]
    assert got["2a"]["answer_rect"] == right[0]
    assert out["name_rect"] == name_b
    # the missed 2b box and the ID box are reported, not silently dropped
    assert right[1] in out["unmatched_boxes"] and id_b in out["unmatched_boxes"]
    assert any(q.get("unlabeled") and q["answer_rect"] == right[1] for q in out["questions"])
    # question areas sit above their box, inside the same column; 1b holds only part (b):
    # it starts below 1a's answer box (the stem and part (a) are sent as context instead)
    for lab, box in (("1a", left[0]), ("1b", left[1]), ("2a", right[0])):
        qr = got[lab]["question_rect"]
        assert qr is not None and qr[1] + qr[3] <= box[1] + 5, lab
        assert (qr[0] + qr[2] / 2 < 1275) == (box[0] < 1000), lab
    assert got["1b"]["question_rect"][1] >= left[0][1] + left[0][3]


# ---------- alignment of real scans ----------
@pytest.mark.skipif(not SCANS_PDF.exists(), reason="private class scans not available")
@pytest.mark.parametrize("student", [0, 9, 15])
def test_real_scans_align(template_pages, student):
    for p in range(1, 5):
        scan = _render(SCANS_PDF, student * 8 + (p - 1) * 2)      # double-sided: fronts are even indexes
        aligned, inliers = align_with_score(template_pages[p], scan)
        assert aligned is not None and inliers >= MIN_ALIGN_INLIERS, (student, p, inliers)
        # top-left corners of the printed boxes land within ~1.5 mm (300 dpi) of the template's
        tb, sb = find_drawn_boxes(template_pages[p]), find_drawn_boxes(aligned)
        for b in tb:
            m = max(sb, key=lambda s: iou(s, b))
            assert iou(m, b) > 0.8, (student, p, b, m)
            assert abs(m[0] - b[0]) <= 15 and abs(m[1] - b[1]) <= 15, (student, p, b, m)


def test_wrong_page_does_not_align_better(template_pages):
    """Version/page detection relies on inliers: a page must match itself far better than another page."""
    same = align_with_score(template_pages[2], template_pages[2])[1]
    other = align_with_score(template_pages[2], template_pages[3])[1]
    assert same > 3 * max(other, 1)


# ---------- live LLM ----------
def _lm_studio_up():
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:1234/v1/models", timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.skipif(os.environ.get("LMGRADER_LIVE") != "1" or not _lm_studio_up(),
                    reason="live layout test: set LMGRADER_LIVE=1 with LM Studio running")
@pytest.mark.parametrize("page", [1, 2])
def test_live_detect_layout(template_pages, page):
    from lmgrader.llm.client import LLMClient, image_to_b64, downscale_for_llm
    img = template_pages[page]
    H, W = img.shape[:2]
    small, sw, sh = downscale_for_llm(img)
    llm = LLMClient(model=os.environ.get("LMGRADER_TEST_MODEL", "qwen3-vl-8b-instruct"), max_tokens=4096)
    lay = refine_layout(img, llm.detect_layout(image_to_b64(small), W, H, sw, sh, columns=2))
    found = {q["problem"] for q in lay["questions"]}
    exp = EXPECTED_REGIONS[page]
    hits = [e for e in exp if e in found]
    if page == 1:
        hits += [k for k in ("name_rect", "id_rect") if lay.get(k)]
        exp = exp + ["name_rect", "id_rect"]
    assert len(hits) >= 0.9 * len(exp), (found, lay.get("name_rect"), lay.get("id_rect"))
    # every answer box sits on a printed rectangle
    boxes = find_drawn_boxes(img)
    for q in lay["questions"]:
        if q["problem"] in exp:
            assert max(iou(q["answer_rect"], b) for b in boxes) > 0.95, q
