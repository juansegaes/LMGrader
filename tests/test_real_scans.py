"""Real data: tests/fixtures/test2a.pdf (blank 4-page exam) + tests/fixtures/private/class_scans.pdf
(20 students x 8 pages, scanned double-sided: front, back, front, back, ...).

The offline tests run whenever the private fixtures are present. The live LLM test (reading
student IDs / names of all 20 students) takes ~10 minutes, so it only runs with
LMGRADER_REAL_LIVE=1 and LM Studio up:

    $env:LMGRADER_REAL_LIVE=1; python -m pytest -q -s tests/test_real_scans.py
"""
import os
import time
from collections import Counter

import pytest

import real_data as rd

pytestmark = pytest.mark.skipif(not rd.available(), reason="private real-data fixtures not present")

EXAM = "Real Test 2a"


@pytest.fixture(scope="module")
def real_template():
    from lmgrader import db
    from lmgrader.models import double_sided_layout
    from lmgrader.template.template import create_template_from_image, persist
    pages = rd.template_pages()
    t = create_template_from_image("Real Test 2a vA", pages, EXAM, "A")
    t.name_rect, t.id_rect = rd.find_header_boxes(pages[0])
    t.name_page = t.id_page = 0
    t.scan_layout = double_sided_layout(t.page_count)
    t.id = persist(t)
    yield db.load_template(t.id)
    db.delete_template(t.id)


@pytest.fixture(scope="module")
def real_subs(real_template):
    from lmgrader import db
    from lmgrader.grading import session
    scans = rd.scan_pages()
    subs, left = session.create_submissions(EXAM, scans, real_template, source="class_scans.pdf")
    yield subs, left, scans
    for s in subs:
        db.delete_submission(s.id)


def test_template_fields_found(real_template):
    assert real_template.page_count == 4 and real_template.scan_page_count == 8
    (nx, ny, nw, nh), (ix, iy, iw, ih) = real_template.name_rect, real_template.id_rect
    assert nx < ix and abs(ny - iy) < 10 and nw > 900 and iw > 900   # two boxes side by side


def test_class_pdf_splits_into_20_tests(real_subs):
    subs, left, scans = real_subs
    assert len(scans) == 160 and len(subs) == 20 and left == []
    for k, s in enumerate(subs):
        base = 8 * k
        assert s.pages == [scans[base + 2 * i] for i in range(4)]          # fronts: even positions
        assert s.back_pages == [scans[base + 2 * i + 1] for i in range(4)]  # backs: odd positions


def test_every_front_page_aligns(real_subs, real_template):
    from lmgrader.config import MIN_ALIGN_INLIERS
    from lmgrader.template.alignment import read_image, align_with_score
    subs, _, _ = real_subs
    t_imgs = [read_image(p) for p in real_template.pages]
    weak = []
    for k, s in enumerate(subs):
        for p, path in enumerate(s.pages):
            aligned, n = align_with_score(t_imgs[p], read_image(path))
            if aligned is None or n < MIN_ALIGN_INLIERS:
                weak.append((k + 1, p + 1, n))
    assert not weak, f"weak alignment (test, page, inliers): {weak}"


def test_header_crops_contain_handwriting(real_subs, real_template):
    """Field crops of a few tests: the name box always has ink; the ID box is empty for the
    students who left it blank (tests 10, 14, 15)."""
    from lmgrader.grading.identify import field_crops
    from lmgrader.template.alignment import to_gray
    subs, _, _ = real_subs

    def ink(img):
        g = to_gray(img)
        h, w = g.shape
        inner = g[int(0.25 * h):int(0.75 * h), int(0.03 * w):int(0.97 * w)]   # skip box borders
        return float((inner < 128).mean())

    for k in (0, 9, 13):
        name, sid = field_crops(real_template, subs[k].pages)
        assert ink(name) > 0.004, f"test {k + 1}: no ink in name box"
        if k in (9, 13):
            assert ink(sid) < 0.002, f"test {k + 1}: ID box should be blank"
        else:
            assert ink(sid) > 0.004, f"test {k + 1}: no ink in ID box"


def _lm_studio_model():
    import json, urllib.request
    try:
        with urllib.request.urlopen("http://localhost:1234/v1/models", timeout=3) as r:
            ids = [m["id"] for m in json.load(r)["data"]]
    except Exception:
        return None
    want = os.environ.get("LMGRADER_TEST_MODEL", "qwen3-vl-8b-instruct")
    return want if want in ids else None


@pytest.mark.skipif(os.environ.get("LMGRADER_REAL_LIVE") != "1", reason="set LMGRADER_REAL_LIVE=1 (takes ~10 min)")
def test_live_identify_all_students(real_subs, real_template):
    model = _lm_studio_model()
    if not model:
        pytest.skip("LM Studio not running or model not loaded")
    from lmgrader.grading import session
    from lmgrader.llm.client import LLMClient
    from lmgrader.models import SUB_READY
    subs, _, _ = real_subs
    roster = rd.load_roster()
    for i, s in enumerate(roster):
        s.id = 9000 + i                         # in-memory ids; the shared test DB roster is untouched
    by_name = {s.name: s for s in roster}
    expected = rd.expected_names()
    llm = LLMClient(model=model, max_tokens=1024)
    stats, rows = Counter(), []
    t0 = time.time()
    for k, sub in enumerate(subs):
        sub.name_confirmed, sub.student_id = False, None
        res = session.identify_submission(sub, [real_template], roster, llm)
        got = next((s.name for s in roster if s.id == res.student_id), None)
        ok = got == expected[k]
        auto = res.status == SUB_READY
        if auto and ok:
            stats["auto_correct"] += 1
        elif auto and not ok:
            stats["auto_WRONG"] += 1
        elif ok:
            stats["suggested_correct_needs_confirm"] += 1
        else:
            stats["needs_human"] += 1
        stats["id_read_exact"] += res.id_read == by_name[expected[k]].student_id
        rows.append((k + 1, res.id_read, res.name_read, ok, auto))
    print(f"\nlive identification of {len(subs)} tests in {time.time() - t0:.0f}s: {dict(stats)}")
    for r in rows:
        print("  test %2d  id_read=%-10s name_read=%-24s correct=%s auto=%s" % r)
    # a confidently auto-assigned wrong student is the one outcome that must never happen
    assert stats["auto_WRONG"] == 0
    assert stats["auto_correct"] >= 15
