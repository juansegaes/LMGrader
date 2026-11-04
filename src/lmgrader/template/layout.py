"""Refine LLM-detected template regions with plain OpenCV.

Vision models place boxes roughly right but not precisely, so:
  * answer / name / ID boxes snap to rectangles printed on the page (found from line strokes);
  * a question area is the printed text above its answer box in the same column; later
    sub-parts in the same column include the shared stem (e.g. 1b gets the setup of 1);
  * open answer areas (no drawn box, e.g. true/false bubbles) span to the next question;
  * questions are put in reading order by label, or column by column.
"""
import re
from typing import List, Optional, Tuple
import cv2
import numpy as np
from .alignment import to_gray

Rect = Tuple[int, int, int, int]

def _ink(img: np.ndarray) -> np.ndarray:
    g = to_gray(img)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw

def line_mask(img: np.ndarray, min_frac: float = 0.06) -> np.ndarray:
    """Long horizontal + vertical strokes only (box outlines, rules), text removed.
    A fixed light threshold keeps faint/grey printed lines that Otsu would drop."""
    g = to_gray(img)
    H, W = g.shape[:2]
    bw = (g < 200).astype(np.uint8) * 255
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, int(W * min_frac)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, int(H * min_frac * 0.5))))
    lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    return cv2.dilate(lines, np.ones((5, 5), np.uint8))

def _side_coverage(mask: np.ndarray, r: Rect, band: int = 6) -> List[float]:
    x, y, w, h = r
    H, W = mask.shape[:2]
    def cov(region, axis):
        if region.size == 0:
            return 0.0
        return float((region.max(axis=axis) > 0).mean())
    top = mask[max(0, y - band):y + band, x:x + w]
    bottom = mask[max(0, y + h - band):min(H, y + h + band), x:x + w]
    left = mask[y:y + h, max(0, x - band):x + band]
    right = mask[y:y + h, max(0, x + w - band):min(W, x + w + band)]
    return [cov(top, 0), cov(bottom, 0), cov(left, 1), cov(right, 1)]

def dedupe_boxes(boxes: List[Rect], thr: float = 0.8) -> List[Rect]:
    out: List[Rect] = []
    for b in sorted(boxes, key=lambda r: -r[2] * r[3]):
        if all(iou(b, o) < thr for o in out):
            out.append(b)
    return out

def find_drawn_boxes(img: np.ndarray, min_area_frac: float = 0.002) -> List[Rect]:
    """Rectangles printed on the page (answer/name/ID boxes), as (x, y, w, h).
    Built from long line strokes, so a box with one faint or broken side (common in PDF renders
    and scans) is still found: at least 3 sides must be ≥70% covered by line ink."""
    mask = line_mask(img)
    H, W = mask.shape[:2]
    min_area = min_area_frac * W * H
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        r = cv2.boundingRect(c)
        x, y, w, h = r
        if w * h < min_area or w > 0.95 * W or h > 0.95 * H or w < 40 or h < 40:
            continue
        # contour of a dilated line hugs the stroke: shrink to the stroke centre
        r = (x + 2, y + 2, max(1, w - 4), max(1, h - 4))
        cov = _side_coverage(mask, r)
        if sum(c_ >= 0.7 for c_ in cov) >= 3 and cov[2] >= 0.7 and cov[3] >= 0.7:
            boxes.append(r)
    return dedupe_boxes(boxes)

def iou(a: Rect, b: Rect) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def snap_rect(rect: Rect, boxes: List[Rect], min_iou: float = 0.3) -> Optional[Rect]:
    """Best-overlapping drawn box if close enough, else None."""
    best = max(boxes, key=lambda b: iou(rect, b), default=None)
    return best if best is not None and iou(rect, best) >= min_iou else None

def _center_in(r: Rect, box: Rect) -> bool:
    cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
    return box[0] <= cx <= box[0] + box[2] and box[1] <= cy <= box[1] + box[3]

def match_score(rect: Rect, box: Rect) -> float:
    """How well an LLM rect corresponds to a drawn box: IoU, or containment either way
    (models often return just the inner/blank part, or the box plus its label)."""
    s = iou(rect, box)
    if _center_in(rect, box) or _center_in(box, rect):
        s = max(s, 0.25)
    return s

def snap_field(rect: Optional[Rect], boxes: List[Rect], page_h: int) -> Optional[Rect]:
    """Name / ID field: the overlapping box, else the nearest box just below the label."""
    if not rect or not boxes:
        return None
    best = max(boxes, key=lambda b: match_score(rect, b))
    if match_score(rect, best) >= 0.2:
        return best
    below = [b for b in boxes if 0 <= b[1] - rect[1] <= 0.08 * page_h
             and min(rect[0] + rect[2], b[0] + b[2]) - max(rect[0], b[0]) > 0]
    return min(below, key=lambda b: b[1] - rect[1]) if below else None

def fit_to_ink(rect: Rect, ink: np.ndarray, pad: int = 8, gap: int = 120, search: int = 0) -> Rect:
    """Fit a text box to the printed text in its band: extends across words, stops at big gaps.
    If the band is empty (box placed slightly off), retries with a band widened by the box height."""
    H, W = ink.shape[:2]
    x, y, w, h = rect
    y0, y1 = max(0, y - pad - search), min(H, y + h + pad + search)
    band = ink[y0:y1]
    cols = np.flatnonzero(band.sum(axis=0))
    if cols.size == 0 or not np.any(band[:, max(0, x - pad):x + w + pad]):
        return fit_to_ink(rect, ink, pad, gap, search=max(h, 30)) if not search else rect
    # group ink columns into runs separated by large gaps; keep runs touching the rect
    runs, start = [], cols[0]
    for a, b in zip(cols[:-1], cols[1:]):
        if b - a > gap:
            runs.append((start, a)); start = b
    runs.append((start, cols[-1]))
    hit = [(a, b) for a, b in runs if b >= x - pad and a <= x + w + pad]
    if not hit:
        return fit_to_ink(rect, ink, pad, gap, search=max(h, 30)) if not search else rect
    xa, xb = min(a for a, _ in hit), max(b for _, b in hit)
    rows = np.flatnonzero(ink[y0:y1, xa:xb + 1].sum(axis=1))
    ya, yb = (y0 + rows[0], y0 + rows[-1]) if rows.size else (y, y + h)
    xa, ya = max(0, xa - pad), max(0, ya - pad)
    xb, yb = min(W, xb + pad), min(H, yb + pad)
    return (int(xa), int(ya), int(xb - xa), int(yb - ya))

def ink_bbox(ink: np.ndarray, x0: int, y0: int, x1: int, y1: int, pad: int = 10) -> Optional[Rect]:
    """Bounding box of the ink inside a zone, or None if the zone is empty."""
    H, W = ink.shape[:2]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    pts = cv2.findNonZero(ink[y0:y1, x0:x1])
    if pts is None:
        return None
    x, y, w, h = cv2.boundingRect(pts)
    xa, ya = max(0, x0 + x - pad), max(0, y0 + y - pad)
    xb, yb = min(W, x0 + x + w + pad), min(H, y0 + y + h + pad)
    return (int(xa), int(ya), int(xb - xa), int(yb - ya))

def content_bounds(ink: np.ndarray) -> Rect:
    pts = cv2.findNonZero(ink)
    if pts is None:
        h, w = ink.shape[:2]
        return (0, 0, w, h)
    return cv2.boundingRect(pts)

def horizontal_rules(img: np.ndarray, min_frac: float = 0.5) -> List[int]:
    """y of long horizontal lines that are not part of a box (section separators)."""
    g = to_gray(img)
    H, W = g.shape[:2]
    bw = (g < 200).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (int(W * min_frac), 1))
    lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)
    ys = np.flatnonzero(lines.max(axis=1))
    out, last = [], -100
    for y in ys:
        if y - last > 10:
            out.append(int(y))
        last = y
    return out

# ---------- labels and reading order ----------
_NUM_LABEL = re.compile(r"^\s*(?:q(?:uestion)?|problem|ex(?:ercise)?)?\s*\.?\s*(\d+)\s*[\.\):]?\s*\(?\s*([a-z]|[ivx]{1,4})?\s*\)?\s*\.?\s*$", re.I)
_PART_LABEL = re.compile(r"^\s*\(?\s*([a-z]|[ivx]{1,4})\s*\)?\s*\.?\s*$", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

def parse_label(label: str) -> Optional[Tuple[int, str]]:
    """'1a', '1 (a)', 'Q2.b', '3' -> (number, part letter or ''). None if not of that shape."""
    m = _NUM_LABEL.match(label or "")
    if not m:
        return None
    return int(m.group(1)), (m.group(2) or "").lower()

def _part_key(part: str) -> int:
    """'' < 'a' < 'b' < ... ; multi-letter roman parts ('ii', 'iv') after single letters."""
    if not part:
        return 0
    return ord(part) - 96 if len(part) == 1 else 100 + _ROMAN.get(part, 0)

def _anchor(q: dict) -> Rect:
    return q.get("answer_rect") or q.get("question_rect") or (0, 0, 1, 1)

def page_columns(W: int, rects: List[Rect], columns: int = 0) -> List[Tuple[int, int]]:
    """Column spans [(x0, x1)]. columns=0: auto (2 if some regions live entirely in each half)."""
    n = int(columns or 0)
    if n <= 0:
        narrow = [r for r in rects if r[2] < 0.6 * W]
        left = any(r[0] + r[2] <= 0.55 * W for r in narrow)
        right = any(r[0] >= 0.45 * W for r in narrow)
        n = 2 if left and right else 1
    if n == 1:
        return [(0, W)]
    if n == 2:
        lefts = [r[0] + r[2] for r in rects if r[0] + r[2] <= 0.55 * W]
        rights = [r[0] for r in rects if r[0] >= 0.45 * W]
        split = int((max(lefts) + min(rights)) / 2) if lefts and rights and min(rights) > max(lefts) else W // 2
        return [(0, split), (split, W)]
    step = W / n
    return [(int(i * step), int((i + 1) * step)) for i in range(n)]

def column_of(r: Rect, cols: List[Tuple[int, int]]) -> int:
    cx = r[0] + r[2] / 2
    for i, (a, b) in enumerate(cols):
        if a <= cx < b:
            return i
    return len(cols) - 1

def normalize_labels(qs: List[dict], W: int, columns: int = 0) -> None:
    """Combine parent number and part letter ('(b)' after '1a' -> '1b'). qs must be in reading order."""
    last_num = None
    for q in qs:
        lab = str(q.get("problem") or "").strip()
        p = parse_label(lab)
        if p:
            last_num = p[0]
            q["problem"] = f"{p[0]}{p[1]}"
            continue
        m = _PART_LABEL.match(lab)
        if m and last_num is not None:
            q["problem"] = f"{last_num}{m.group(1).lower()}"

def order_questions(qs: List[dict], W: int, columns: int = 0) -> List[dict]:
    """Reading order: by label (1a < 1b < 2a < ... < 10) when every label parses and is unique,
    otherwise column by column, top to bottom (columns hint, or auto-detected)."""
    cols = page_columns(W, [_anchor(q) for q in qs], columns)
    spatial = sorted(qs, key=lambda q: (column_of(_anchor(q), cols), _anchor(q)[1], _anchor(q)[0]))
    normalize_labels(spatial, W, columns)
    keys = [parse_label(q.get("problem", "")) for q in spatial]
    if spatial and all(keys) and len(set(keys)) == len(keys):
        return [q for _, q in sorted(zip(keys, spatial), key=lambda kq: (kq[0][0], _part_key(kq[0][1])))]
    return spatial

# ---------- refinement ----------
def refine_layout(img: np.ndarray, layout: dict, columns: Optional[int] = None) -> dict:
    """Snap answer / name / ID boxes to the drawn rectangles, derive each question area from the
    printed text above its box (same column), order questions, and expand open answer areas
    (no drawn box, e.g. true/false bubbles) down to the next question of the column.
    Mutates and returns layout; adds layout['unmatched_boxes'] (drawn boxes nobody claimed)."""
    H, W = img.shape[:2]
    if columns is None:
        columns = int(layout.get("columns") or 0)
    ink = _ink(img)
    boxes = find_drawn_boxes(img)
    rules = horizontal_rules(img)
    for bx, by, bw, bh in boxes:      # box outlines are not question text
        cv2.rectangle(ink, (bx, by), (bx + bw, by + bh), 0, 12)
    for y in rules:
        ink[max(0, y - 6):y + 7] = 0
    qs = [q for q in layout.get("questions", []) if q.get("question_rect") or q.get("answer_rect")]
    claimed = set()

    # 1. name / ID fields
    for key in ("name_rect", "id_rect"):
        snapped = snap_field(layout.get(key), [b for b in boxes if b not in claimed], H)
        if snapped:
            layout[key] = snapped
            claimed.add(snapped)

    # 2. answer boxes: greedy best match first
    pairs = sorted(((match_score(q["answer_rect"], b), i, b) for i, q in enumerate(qs) if q.get("answer_rect")
                    for b in boxes), key=lambda t: -t[0])
    snapped_q = {}
    for s, i, b in pairs:
        if s < 0.2 or i in snapped_q or b in claimed:
            continue
        snapped_q[i] = b
        claimed.add(b)
    for i, b in snapped_q.items():
        qs[i]["answer_rect"] = b
        qs[i]["snapped"] = True

    # 3. drawn boxes nobody claimed (the model missed a question)
    unmatched = [b for b in boxes if b not in claimed]
    layout["unmatched_boxes"] = unmatched
    for b in unmatched:
        qs.append({"problem": "?", "question_rect": None, "answer_rect": b, "points": None,
                   "snapped": True, "unlabeled": True})

    cols = page_columns(W, [_anchor(q) for q in qs] + boxes, columns)
    col_ink = []
    for a, b_ in cols:
        m = np.zeros_like(ink)
        m[:, a:b_] = ink[:, a:b_]
        col_ink.append(m)

    # 4. question areas
    for q in qs:
        a = q["answer_rect"] if q.get("snapped") else None
        c = column_of(_anchor(q), cols)
        x0, x1 = cols[c]
        if a:
            ax, ay, aw, ah = a
            above = [o[1] + o[3] for o in boxes if o != a and o[1] + o[3] <= ay + 5
                     and min(ax + aw, o[0] + o[2]) - max(ax, o[0]) > 0.3 * aw]
            above += [y for y in rules if y <= ay]
            zone_top = max(above, default=0)
            qr = ink_bbox(col_ink[c], x0, zone_top + 4, x1, ay - 4)
            if qr:
                q["question_rect"] = qr
        elif q.get("question_rect"):
            q["question_rect"] = fit_to_ink(q["question_rect"], col_ink[c])

    qs = order_questions(qs, W, columns)
    # 5. sub-parts: each question area holds only its own part. The first part's area also holds
    # the shared stem (it is the text above it); the stem and earlier parts are sent as context
    # when grading (template.question_context), so later parts don't need them in their box.

    # 6. open answer areas (no drawn box): from the question to the next one in the column
    by_col = {}
    for q in qs:
        by_col.setdefault(column_of(_anchor(q), cols), []).append(q)
    cx, cy, cw, ch = content_bounds(ink)
    for c, members in by_col.items():
        members.sort(key=lambda q: (q.get("question_rect") or _anchor(q))[1])
        tops = [(q.get("question_rect") or _anchor(q))[1] for q in members]
        gaps = [b - a for a, b in zip(tops[:-1], tops[1:]) if b > a]
        typical = int(np.median(gaps)) if gaps else H // 4
        x0, x1 = cols[c]
        for j, q in enumerate(members):
            if q.get("snapped"):
                continue
            qr = q.get("question_rect")
            top = qr[1] if qr else _anchor(q)[1]
            nxt = next((t for t in tops[j + 1:] if t > top + 20), None)
            if nxt is not None:
                bottom = nxt - 6
            else:
                model_bottom = q["answer_rect"][1] + q["answer_rect"][3] if q.get("answer_rect") else 0
                bottom = min(H - 20, max(model_bottom, top + typical - 10))
            left = max(x0, min(qr[0] if qr else x1, q["answer_rect"][0] if q.get("answer_rect") else x1) - 10)
            right = min(x1, max(cx + cw, int(x1 * 0.97)) if c == len(cols) - 1 else x1 - 10)
            if bottom - top >= 20 and right - left > 40:
                q["answer_rect"] = (int(left), int(top), int(right - left), int(bottom - top))
    for q in qs:
        q.pop("snapped", None)
    layout["questions"] = qs
    return layout
