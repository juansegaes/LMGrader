"""Back-page ('work continued on back') detection.

Scanned back sides are rarely truly blank: scanners leave a dark line along an edge, dust
specks, and filled bubbles or heavy writing bleed through from the front as faint dots. So
instead of counting dark pixels we look for *handwriting-like blobs*:

1. normalise the page to BACK_REF_WIDTH px wide and drop a margin band (edge lines);
2. binarise and remove specks with a morphological opening;
3. dilate so the strokes of a word merge into one blob, and keep only blobs that are large
   enough (lone dots / bleed-through bubbles are small and isolated);
4. classify by the total ink inside the kept blobs: 'blank' | 'minor' | 'work'.
"""
from typing import List, Tuple
import cv2
import numpy as np
from ..template.alignment import read_image, to_gray
from ..config import (BACK_REF_WIDTH, BACK_MARGIN, BACK_INK_GRAY, BACK_MIN_BLOB, BACK_MIN_BLOB_INK,
                      BACK_MINOR_INK, BACK_WORK_INK)

LEVEL_BLANK, LEVEL_MINOR, LEVEL_WORK = "blank", "minor", "work"

def analyze_back_image(img: np.ndarray) -> dict:
    """Returns {level, has_work, flagged, ink, blobs: [(x, y, w, h) in original pixels], reason}."""
    g = to_gray(img)
    h0, w0 = g.shape[:2]
    s = BACK_REF_WIDTH / float(w0)
    small = cv2.resize(g, (BACK_REF_WIDTH, max(1, int(round(h0 * s)))), interpolation=cv2.INTER_AREA)
    h, w = small.shape
    mx, my = int(BACK_MARGIN * w), int(BACK_MARGIN * h)
    core = small[my:h - my, mx:w - mx]
    ink = (core < BACK_INK_GRAY).astype(np.uint8)
    # drop isolated specks (a morphological opening would also erase thin pencil strokes)
    n0, lab0, st0, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    tiny = np.zeros(n0, bool)
    tiny[1:] = st0[1:, cv2.CC_STAT_AREA] < 3
    ink[tiny[lab0]] = 0
    # merge strokes of a word/line into one blob
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 7)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    blobs: List[Tuple[int, int, int, int]] = []
    total = 0
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        # dilation adds ~14 x 6 px; measure the real extent of the ink inside the blob
        if max(bw - 14, bh - 6) < BACK_MIN_BLOB:
            continue
        blob_ink = int(ink[labels == i].sum())
        if blob_ink < BACK_MIN_BLOB_INK:
            continue
        total += blob_ink
        blobs.append((int((x + mx) / s), int((y + my) / s), int(bw / s), int(bh / s)))
    if total >= BACK_WORK_INK:
        level = LEVEL_WORK
    elif total >= BACK_MINOR_INK:
        level = LEVEL_MINOR
    else:
        level = LEVEL_BLANK
    flagged = level == LEVEL_WORK
    return {"level": level, "has_work": flagged, "flagged": flagged, "ink": total, "blobs": blobs,
            "ink_ratio": total / float(max(1, core.size)),
            "reason": "significant_ink_on_back" if flagged else ("minor_marks_on_back" if level == LEVEL_MINOR else "")}

def analyze_back_page(path: str) -> dict:
    """Returns dict: level ('blank'|'minor'|'work'), has_work, flagged, ink, blobs, reason."""
    img = read_image(path) if path else None
    if img is None:
        return {"level": LEVEL_BLANK, "has_work": False, "ink_ratio": 0.0, "ink": 0, "blobs": [],
                "flagged": False, "reason": "no_image"}
    return analyze_back_image(img)

def annotate_back_review_needed(submission) -> bool:
    return bool(submission.has_work_on_back and not submission.back_reviewed)
