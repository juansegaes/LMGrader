"""Multiple choice marks without an LLM: optical mark recognition against the blank exam.

Each choice of a multiple choice question has a box on the blank template page: the printed
bubble with its label ("○ True"), or the printed answer students circle ("(B) 4"). Scans are
aligned to the template, so the ink a student added in a box is the scan's ink minus the
template's ink (grown by a few pixels for small alignment errors). The choices with clearly the
most added ink are the marked ones; a partly marked choice (an erased or crossed-out mark) makes
the answer unsure and leaves it to the human. In short: each submission is overlaid on the blank
exam and only the differences count.

Plain OpenCV/NumPy: no model, milliseconds per test.
"""
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

Rect = Tuple[int, int, int, int]

INK = 150             # gray level below which a pixel is ink
TOLERANCE = 3         # template ink is grown by this many pixels (alignment error)
# a choice's mark = added ink pixels / (box height)²: independent of how wide the box is
MIN_MARK = 0.03       # below this the choice is not marked (unmarked boxes score ~0.00-0.01; a thin
                      # tick ~0.04-0.07, a filled bubble ~0.15)
MARK_RATIO = 0.5      # marked: at least this fraction of the most marked choice
UNSURE_RATIO = 0.25   # a choice between UNSURE_RATIO and MARK_RATIO of the top one: unsure

# ---------------------------------------------------------------- measuring

def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

def _clip(rect: Sequence[int], shape) -> Optional[Rect]:
    H, W = shape[:2]
    x, y, w, h = (int(v) for v in rect)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None

def same_size(scan: np.ndarray, template: np.ndarray) -> np.ndarray:
    """The scan at the template's pixel size (an aligned page already is)."""
    if scan.shape[:2] != template.shape[:2]:
        scan = cv2.resize(scan, (template.shape[1], template.shape[0]), interpolation=cv2.INTER_AREA)
    return scan

def added_ink_mask(scan: np.ndarray, template: np.ndarray, rect: Sequence[int],
                   tolerance: int = TOLERANCE) -> np.ndarray:
    """Ink in `rect` of the (aligned) scan that is not printed on the template (uint8 0/1)."""
    scan = same_size(scan, template)
    r = _clip(rect, template.shape)
    if r is None:
        return np.zeros((1, 1), np.uint8)
    x, y, w, h = r
    s = _gray(scan)[y:y + h, x:x + w]
    t = _gray(template)[y:y + h, x:x + w]
    k = 2 * tolerance + 1
    printed = cv2.dilate((t < INK).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    new = ((s < INK) & (printed == 0)).astype(np.uint8)
    return cv2.morphologyEx(new, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))     # specks

def mark_score(scan: np.ndarray, template: np.ndarray, rect: Sequence[int]) -> float:
    """How much a student marked one choice box: added ink / (box height)²."""
    h = max(1, int(rect[3]))
    return float(added_ink_mask(scan, template, rect).sum()) / (h * h)

def decide(scores: Sequence[float]) -> Optional[List[int]]:
    """Indices of the marked choices; [] = nothing marked (blank); None = unsure (the human looks)."""
    if not scores:
        return None
    top = max(scores)
    if top < MIN_MARK:
        return []
    cut = max(MIN_MARK, MARK_RATIO * top)
    marked = [i for i, s in enumerate(scores) if s >= cut]
    if any(max(MIN_MARK / 2, UNSURE_RATIO * top) <= s < cut for s in scores):
        return None
    return marked

def student_ink(scan: np.ndarray, template: np.ndarray, rect: Sequence[int], pad: int = 0) -> np.ndarray:
    """Crop of `rect` (+pad) with only what the student wrote: printed text, lines and boxes of
    the template are whitened (BGR). Used to read short answers."""
    scan = same_size(scan, template)
    x, y, w, h = (int(v) for v in rect)
    r = _clip((x - pad, y - pad, w + 2 * pad, h + 2 * pad), template.shape)
    if r is None:
        return np.full((8, 8, 3), 255, np.uint8)
    mask = added_ink_mask(scan, template, r)
    x, y, w, h = r
    crop = _gray(scan)[y:y + h, x:x + w].copy()
    crop[cv2.dilate(mask, np.ones((3, 3), np.uint8)) == 0] = 255
    return cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)

# ---------------------------------------------------------------- finding the choices

def reading_order(rects: List[Rect]) -> List[Rect]:
    """Rows top to bottom (boxes whose centers are within half a box height share a row), then
    left to right."""
    if not rects:
        return []
    hmed = float(np.median([r[3] for r in rects]))
    rows: List[List[Rect]] = []
    for r in sorted(rects, key=lambda r: r[1] + r[3] / 2):
        cy = r[1] + r[3] / 2
        if rows and abs(cy - np.mean([q[1] + q[3] / 2 for q in rows[-1]])) < hmed / 2:
            rows[-1].append(r)
        else:
            rows.append([r])
    return [r for row in rows for r in sorted(row, key=lambda r: r[0])]

def _components(ink: np.ndarray):
    n, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    return [tuple(int(v) for v in stats[i][:4]) for i in range(1, n) if stats[i][4] >= 4]

def _bubbles(ink: np.ndarray) -> List[Rect]:
    """Printed bubbles / check boxes: hollow, roughly square shapes, all about the same size."""
    contours, hier = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    H, W = ink.shape
    cands = []
    for i, c in enumerate(contours):
        if hier[0][i][3] != -1:            # a hole, not an outer contour
            continue
        x, y, w, h = cv2.boundingRect(c)
        if w < 12 or h < 12 or w > 0.5 * W and h > 0.5 * H or not 0.75 <= w / h <= 1.33:
            continue
        child = hier[0][i][2]
        hole = 0.0
        while child != -1:
            hole = max(hole, cv2.contourArea(contours[child]))
            child = hier[0][child][0]
        fill = cv2.contourArea(c) / float(w * h)
        if hole / float(w * h) >= 0.4 and 0.6 <= fill <= 1.05:
            cands.append((x, y, w, h))
    # the bubbles of one question have one size: the biggest group of similar sizes (ties: larger)
    best: List[Rect] = []
    for ref in cands:
        size = (ref[2] + ref[3]) / 2
        group = [r for r in cands if abs((r[2] + r[3]) / 2 - size) <= 0.15 * size]
        if (len(group), size) > (len(best), (best[0][2] + best[0][3]) / 2 if best else 0):
            best = group
    return best if len(best) >= 2 else []

def _with_labels(ink: np.ndarray, bubbles: List[Rect]) -> List[Rect]:
    """Each bubble widened over its printed label on the right ("○ True" as one box)."""
    comps = [c for c in _components(ink)
             if not any(abs(c[0] - b[0]) <= 2 and abs(c[1] - b[1]) <= 2 for b in bubbles)]
    out = []
    for bx, by, bw, bh in bubbles:
        right = bx + bw
        stop = min([b[0] for b in bubbles if b[0] > bx and abs(b[1] - by) < bh / 2] + [ink.shape[1]])
        x0, y0, x1, y1 = bx, by, right, by + bh
        for cx, cy, cw, ch in sorted(comps, key=lambda c: c[0]):
            mid = cy + ch / 2
            if cx < right or cx + cw > stop or not by - bh * 0.25 <= mid <= by + bh * 1.25 or ch > 2.5 * bh:
                continue
            if cx - x1 > 1.5 * bh:
                break
            x1, y0, y1 = max(x1, cx + cw), min(y0, cy), max(y1, cy + ch)
        out.append((x0, y0, x1 - x0, y1 - y0))
    return out

def _text_options(ink: np.ndarray) -> List[Rect]:
    """No bubbles: the printed answers (words / letters on one line separated by wide gaps)."""
    H, W = ink.shape
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, W // 3), 1)))
    lines |= cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, H * 2 // 3))))
    ink = ink & ~cv2.dilate(lines, np.ones((3, 3), np.uint8))
    comps = [c for c in _components(ink) if c[3] >= 6]
    if len(comps) < 2:
        return []
    ch = float(np.median([c[3] for c in comps]))
    merged = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(0.9 * ch)), max(1, int(0.3 * ch)))))
    blobs = [b for b in _components(merged) if b[3] >= 0.6 * ch]
    return blobs if 2 <= len(blobs) <= 12 else []

def detect_choices(template: np.ndarray, rect: Sequence[int]) -> List[Rect]:
    """Choice boxes (template page pixels, reading order) found inside `rect` of the blank page:
    printed bubbles with their labels, else the separate printed answers. [] if none found."""
    r = _clip(rect, template.shape)
    if r is None:
        return []
    x, y, w, h = r
    ink = (_gray(template)[y:y + h, x:x + w] < INK).astype(np.uint8) * 255
    bubbles = _bubbles(ink)
    if bubbles:
        boxes, pad = _with_labels(ink, bubbles), 0.3
    else:
        boxes, pad = _text_options(ink), 0.35
    out = []
    for bx, by, bw, bh in boxes:
        m = int(round(pad * bh))
        out.append((x + bx - m, y + by - m, bw + 2 * m, bh + 2 * m))
    inner = [(x + bx, y + by, bw, bh) for bx, by, bw, bh in boxes]
    return reading_order(_separate(out, inner))

def _separate(boxes: List[Rect], inner: List[Rect]) -> List[Rect]:
    """Padded boxes that would overlap (stacked "○ True / ○ False") meet halfway between the
    printed choices instead: a mark near one choice must not count for its neighbor."""
    e = [[b[0], b[1], b[0] + b[2], b[1] + b[3]] for b in boxes]
    for i, a in enumerate(inner):
        for j, b in enumerate(inner):
            if i == j:
                continue
            p, q = e[i], e[j]
            x_overlap = p[0] < q[2] and q[0] < p[2]
            y_overlap = p[1] < q[3] and q[1] < p[3]
            if x_overlap and y_overlap and a[1] + a[3] <= b[1]:          # b below a
                mid = (a[1] + a[3] + b[1]) // 2
                p[3], q[1] = min(p[3], mid), max(q[1], mid)
            elif x_overlap and y_overlap and a[0] + a[2] <= b[0]:        # b right of a
                mid = (a[0] + a[2] + b[0]) // 2
                p[2], q[0] = min(p[2], mid), max(q[0], mid)
    return [(p[0], p[1], p[2] - p[0], p[3] - p[1]) for p in e]

def has_bubbles(template: np.ndarray, rect: Sequence[int]) -> bool:
    """The answer box of the blank page has printed bubbles / check boxes (2 or more)."""
    r = _clip(rect, template.shape)
    if r is None:
        return False
    x, y, w, h = r
    return bool(_bubbles((_gray(template)[y:y + h, x:x + w] < INK).astype(np.uint8) * 255))

def default_labels(n: int) -> List[str]:
    return [chr(ord("A") + i) if i < 26 else str(i + 1) for i in range(n)]
