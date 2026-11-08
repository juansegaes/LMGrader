"""Feedback PDFs returned to students.

One PDF per student: a summary page with the total and every question's score and written
feedback (the text shrinks so it all fits on that one page), then every scanned page (aligned
to the template) with, for each answer region, a thin red outline and its score ("3 / 4").
Built from the database at export time, so it always reflects the current grades.
"""
import csv
import os
import re
import sys
from typing import Callable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from ..db import (list_grades, list_students, load_template, list_versions, list_submissions)
from ..models import Submission, TestTemplate, Grade, Student, SUB_GRADED

RED = (208, 0, 0)
LETTER_WIDTH_IN = 8.5          # used to derive a PDF resolution from the scan width
_HIDDEN_FEEDBACK = ("llm_error", "unparsed_response")   # internal failures are never shown

# ---------- fonts ----------
# Feedback text: fonts with good math-symbol coverage (∈ ± ≤ √ π → …) first.
_TEXT_CANDIDATES = [r"C:\Windows\Fonts\cambria.ttc", r"C:\Windows\Fonts\seguisym.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
                    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]
_FONT_CANDIDATES = {
    False: [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"],
    True: [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
           "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial Bold.ttf"],
}
_font_cache = {}

def _font(size: int, bold: bool = False, text: bool = False):
    key = (size, bold, text)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    paths = (_TEXT_CANDIDATES if text else []) + _FONT_CANDIDATES[bold] + _FONT_CANDIDATES[False]
    for path in paths:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:   # Pillow < 10.1
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f

def _text_h(lines, font, spacing) -> int:
    asc, desc = font.getmetrics()
    return len(lines) * (asc + desc) + max(0, len(lines) - 1) * spacing

def _draw_block(overlay: ImageDraw.ImageDraw, x, y, lines, font, spacing, backing=True, pad=6):
    asc, desc = font.getmetrics()
    lh = asc + desc
    w = max((font.getlength(l) for l in lines), default=0)
    h = _text_h(lines, font, spacing)
    if backing:
        overlay.rounded_rectangle((x - pad, y - pad, x + w + pad, y + h + pad), radius=pad,
                                  fill=(255, 255, 255, 215))
    for i, l in enumerate(lines):
        overlay.text((x, y + i * (lh + spacing)), l, font=font, fill=RED + (255,))

# ---------- helpers ----------
def _load_page(path: str) -> Optional[Image.Image]:
    if path and os.path.exists(path):
        try:
            return Image.open(path).convert("RGB")
        except OSError:
            return None
    return None

_LATEX = {r"\pm": "±", r"\mp": "∓", r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\leq": "≤", r"\le": "≤",
          r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\in": "∈", r"\notin": "∉",
          r"\infty": "∞", r"\pi": "π", r"\theta": "θ", r"\alpha": "α", r"\beta": "β", r"\lambda": "λ",
          r"\mu": "μ", r"\sigma": "σ", r"\Delta": "Δ", r"\delta": "δ", r"\sum": "∑", r"\int": "∫",
          r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\implies": "⇒", r"\sqrt": "√",
          r"\{": "{", r"\}": "}", r"\left": "", r"\right": "", r"\,": " ", r"\;": " ", r"\quad": " "}
_SUP = str.maketrans("0123456789+-n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")
_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

def latex_to_text(s: str) -> str:
    """Make inline LaTeX readable in plain text: $x^2 \\pm 1$ -> x² ± 1, \\frac{a}{b} -> (a)/(b)."""
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", s)
    for k in sorted(_LATEX, key=len, reverse=True):
        guard = r"(?![A-Za-z])" if k[-1].isalpha() else ""   # \in must not eat \infty
        s = re.sub(re.escape(k) + guard, lambda _m, v=_LATEX[k]: v, s)
    s = re.sub(r"\^\{([0-9+\-n]+)\}", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"\^([0-9n])", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_\{?([0-9]+)\}?", lambda m: m.group(1).translate(_SUB), s)
    s = s.replace("$$", "").replace("$", "").replace(r"\[", "").replace(r"\]", "")
    s = s.replace(r"\(", "").replace(r"\)", "")
    return re.sub(r"[ \t]{2,}", " ", s)

# ---------- rendered math in feedback ----------
# $...$, \(...\) inline; $$...$$, \[...\] on their own line. A lone "$5" is not math: the opening $
# must be followed, and the closing $ preceded, by a non-space (and not followed by a digit).
_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|\$(?=\S)(.+?)(?<=\S)\$(?!\d)", re.S)
# commands matplotlib's mathtext lacks -> supported equivalents
_MATHTEXT_FIX = [(r"\implies", r"\Rightarrow"), (r"\iff", r"\Leftrightarrow"), (r"\le", r"\leq"),
                 (r"\ge", r"\geq"), (r"\tfrac", r"\frac"), (r"\displaystyle", ""), (r"\textstyle", ""),
                 (r"\lvert", "|"), (r"\rvert", "|"), (r"\lVert", r"\Vert"), (r"\rVert", r"\Vert"),
                 (r"\textbf", r"\mathbf"), (r"\textit", r"\mathit"), (r"\emph", r"\mathit"),
                 (r"\boxed", ""), (r"\&", r"\ and\ ")]
_math_cache = {}

def _render_math(tex: str, size: int, color) -> Optional[Tuple[Image.Image, int, int]]:
    """TeX math -> (RGBA image in `color`, ascent, descent) via matplotlib mathtext, or None if
    matplotlib is missing or the formula uses something mathtext can't draw."""
    key = (tex, size, color)
    if key in _math_cache:
        return _math_cache[key]
    out = None
    try:
        import matplotlib
        from matplotlib.mathtext import MathTextParser
        from matplotlib.font_manager import FontProperties
        s = tex.strip()
        for k, v in _MATHTEXT_FIX:
            s = re.sub(re.escape(k) + r"(?![A-Za-z])", lambda _m, v=v: v, s)
        with matplotlib.rc_context({"mathtext.fontset": "cm"}):
            r = MathTextParser("agg").parse(f"${s}$", dpi=72, prop=FontProperties(size=size))
        import numpy as np
        alpha = np.asarray(r.image)
        if alpha.size:
            img = Image.new("RGBA", (alpha.shape[1], alpha.shape[0]), tuple(color) + (0,))
            img.putalpha(Image.fromarray(alpha))
            desc = int(round(r.depth))
            out = (img, img.height - desc, desc)
    except Exception:
        out = None
    _math_cache[key] = out
    return out

class _Rich:
    """Feedback text laid out in lines of words and rendered formulas."""
    def __init__(self, lines, width, height):
        self.lines, self.w, self.h = lines, width, height

def _rich_items(text: str, font, color):
    """-> items: ("w", word, space_before) | ("m", image, ascent, descent, space_before) | ("br",)"""
    items, space = [], False
    def words(s):
        nonlocal space
        s = latex_to_text(s.replace("$", "\0")).replace("\0", "$")   # a "$" left here is money, not math
        for tok in re.split(r"(\s+)", s):
            if not tok:
                continue
            if tok.isspace():
                space = True
                if "\n" in tok:
                    items.extend([("br",)] * tok.count("\n"))
                    space = False
            else:
                items.append(("w", tok, space)); space = False
    size = font.size if hasattr(font, "size") else 14
    pos = 0
    for m in _MATH_RE.finditer(text):
        words(text[pos:m.start()])
        tex = next(g for g in m.groups() if g is not None)
        display = m.group(1) is not None or m.group(2) is not None
        rendered = _render_math(tex, size, color)
        if display and items and items[-1] != ("br",):
            items.append(("br",))
        if rendered is None:   # "$5 ... $" was probably money: keep it as written
            words(m.group(0) if re.match(r"\d", tex) else tex)
        else:
            items.append(("m",) + rendered + (space,)); space = False
        if display:
            items.append(("br",)); space = False
        pos = m.end()
    words(text[pos:])
    return items

def _layout_rich(text: str, font, max_w: float, spacing: int, color=RED) -> _Rich:
    asc, desc = font.getmetrics()
    space_w = font.getlength(" ")
    lines, cur, cur_w = [], [], 0.0
    def close():
        la = max([asc] + [it[2] for _, it in cur if it[0] == "m"])
        ld = max([desc] + [it[3] for _, it in cur if it[0] == "m"])
        lines.append((list(cur), cur_w, la, ld))
    for it in _rich_items(text, font, color):
        if it[0] == "br":
            close(); cur, cur_w = [], 0.0
            continue
        iw = font.getlength(it[1]) if it[0] == "w" else it[1].width
        gap = space_w if cur and it[-1] else 0
        if cur and cur_w + gap + iw > max_w:
            close(); cur, cur_w, gap = [], 0.0, 0
        cur.append((cur_w + gap, it)); cur_w += gap + iw
    close()
    width = max((l[1] for l in lines), default=0)
    height = sum(l[2] + l[3] for l in lines) + max(0, len(lines) - 1) * spacing
    return _Rich(lines, width, height)

def _draw_rich(target: Image.Image, x, y, rich: _Rich, font, spacing, color=RED, backing=True, pad=6):
    d = ImageDraw.Draw(target)
    if backing:
        d.rounded_rectangle((x - pad, y - pad, x + rich.w + pad, y + rich.h + pad), radius=pad,
                            fill=(255, 255, 255, 215))
    asc, _ = font.getmetrics()
    fill = tuple(color) + ((255,) if target.mode == "RGBA" else ())
    for items, _w, la, ld in rich.lines:
        base = y + la
        for dx, it in items:
            if it[0] == "w":
                d.text((x + dx, base - asc), it[1], font=font, fill=fill)
            else:
                pos = (max(0, int(round(x + dx))), max(0, int(round(base - it[2]))))
                if target.mode == "RGBA":
                    target.alpha_composite(it[1], dest=pos)
                else:
                    target.paste(it[1], pos, it[1])
        y += la + ld + spacing

PENDING = "Pending review"

def _pending(g: Grade) -> bool:
    """An AI suggestion still waiting for the professor: its score/feedback must not go out."""
    return bool(g.needs_human_review) and not getattr(g, "human_graded", False)

def _score_text(g: Grade) -> str:
    return PENDING if _pending(g) else f"{_fmt(g.score)} / {_fmt(g.max_points)}"

def _total_text(grades: List[Grade]) -> str:
    """Total of the grades a human can stand behind; pending ones are left out and noted."""
    done = [g for g in grades if not _pending(g)]
    txt = f"Total: {_fmt(sum(g.score for g in done))} / {_fmt(sum(g.max_points for g in grades))}"
    n = len(grades) - len(done)
    return txt + (f" ({n} pending review)" if n else "")

def _shown_feedback(g: Grade) -> str:
    if _pending(g):
        return ""
    if g.review_reason and any(h in g.review_reason for h in _HIDDEN_FEEDBACK):
        return ""
    if (g.feedback or "").startswith("LLM error"):
        return ""
    return (g.feedback or "").strip()   # LaTeX kept: math is rendered when drawn

def _region_for(template: Optional[TestTemplate], g: Grade):
    if template is None or g.region_id is None or not (0 <= g.region_id < len(template.regions)):
        return None
    return template.regions[g.region_id]

def _fmt(v: float) -> str:
    return f"{v:g}"

# ---------- rendering ----------
def _annotate_page(img: Image.Image, items: List[Tuple[object, Grade]], header: str = "") -> Image.Image:
    """items: (region, grade) on this page. Returns an RGB copy with each answer box outlined in
    red and its score; the written feedback is only on the summary page."""
    W, H = img.size
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    sc_size = max(16, int(W * 0.028))
    sc_font = _font(sc_size, bold=True)
    spacing = max(2, sc_size // 6)
    margin = int(W * 0.02)
    line_w = max(2, W // 600)

    if header:
        hf = _font(max(16, int(W * 0.022)), bold=True)
        tw = hf.getlength(header)
        _draw_block(d, W - margin - tw, margin, [header], hf, spacing)

    for region, g in items:
        x, y, w, h = region.rect
        d.rectangle((x, y, x + w, y + h), outline=RED + (255,), width=line_w)
        score = _score_text(g)
        sw = sc_font.getlength(score)
        asc, desc = sc_font.getmetrics()
        sh = asc + desc
        sx = x + w - sw if x + w - sw >= 0 else x
        sy = y - sh - 6 if y - sh - 6 >= 0 else y + 6
        _draw_block(d, sx, sy, [score], sc_font, spacing, pad=4)

    return Image.alpha_composite(base, overlay).convert("RGB")

SUMMARY_TEXT_MAX, SUMMARY_TEXT_MIN = 0.018, 0.010   # feedback text size, as a fraction of page width
_FB_COLOR = (40, 40, 40)

def _summary_pages(size, exam: str, version: str, student: Optional[Student], sub: Submission,
                   rows: List[Grade]) -> List[Image.Image]:
    """The score and written feedback of every question, on one page: the text shrinks (down to
    SUMMARY_TEXT_MIN) until all of it fits. Only past that does it continue on another page."""
    W, H = size
    m = int(W * 0.07)
    title, big = _font(int(W * 0.032), True), _font(int(W * 0.026), True)
    col_p, col_s = m, m + int(W * 0.12)
    col_f = m + int(W * 0.27)
    fb_w = W - m - col_f
    head_h = int(W * 0.05) + int(W * 0.035) + int(W * 0.06)   # title, name, total

    def layout(px):
        body, bold = _font(px, text=True), _font(max(8, px - 1), True)
        sp = max(2, px // 5)
        asc, desc = body.getmetrics()
        table_head = asc + desc + sp * 4
        riches = [_layout_rich(_shown_feedback(g) or "—", body, fb_w, sp, color=_FB_COLOR) for g in rows]
        need = head_h + table_head + sum(max(r.h, asc + desc) + sp * 4 for r in riches)
        return body, bold, sp, table_head, riches, need

    sizes = range(int(W * SUMMARY_TEXT_MAX), max(8, int(W * SUMMARY_TEXT_MIN)) - 1, -1)
    for px in sizes:
        body, bold, sp, table_head, riches, need = layout(px)
        if m + need <= H - m:
            break

    pages = []
    def new_page(first):
        page = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(page)
        y = m
        if first:
            d.text((m, y), f"{exam}" + (f"  —  version {version}" if version else ""), font=title, fill="black")
            y += int(W * 0.05)
            name = student.name if student else (sub.name_read or "Unassigned")
            sid = f"   ·   ID {student.student_id}" if student and student.student_id else ""
            d.text((m, y), f"{name}{sid}", font=body, fill="black")
            y += int(W * 0.035)
            d.text((m, y), _total_text(rows), font=big, fill=RED)
            y += int(W * 0.06)
        d.text((col_p, y), "Problem", font=bold, fill="black")
        d.text((col_s, y), "Score", font=bold, fill="black")
        d.text((col_f, y), "Feedback" + ("" if first else " (continued)"), font=bold, fill="black")
        y += table_head - sp * 2
        d.line((m, y, W - m, y), fill=(180, 180, 180), width=max(1, W // 1200))
        pages.append(page)
        return page, d, y + sp * 2

    page, d, y = new_page(True)
    asc, desc = body.getmetrics()
    for g, rich in zip(rows, riches):
        need = max(rich.h, asc + desc) + sp
        if y + need > H - m and y > m + table_head + sp * 2:   # only when nothing fits: continue
            page, d, y = new_page(False)
        d.text((col_p, y), g.problem, font=body, fill="black")
        d.text((col_s, y), _score_text(g), font=body, fill=RED)
        _draw_rich(page, col_f, y, rich, body, sp, color=_FB_COLOR, backing=False)
        y += need + sp * 3
        d.line((m, y - sp * 2, W - m, y - sp * 2), fill=(230, 230, 230), width=1)
    return pages

def render_feedback_pages(submission: Submission, template: Optional[TestTemplate], grades: List[Grade],
                          student: Optional[Student], summary: bool = True) -> List[Image.Image]:
    aligned = submission.aligned_pages or ([submission.aligned_front_path] if submission.aligned_front_path else [])
    scans = submission.pages
    n_pages = max(len(scans), template.page_count if template else 0, 1)
    by_page = {}
    for g in grades:
        r = _region_for(template, g)
        if r is not None:
            by_page.setdefault(r.page, []).append((r, g))
    name = student.name if student else (submission.name_read or "")
    out = []
    for p in range(n_pages):
        img = _load_page(aligned[p] if p < len(aligned) else "") or _load_page(scans[p] if p < len(scans) else "")
        if img is None:
            continue
        header = f"{name}   {_total_text(grades)}".strip() if p == 0 else ""
        out.append(_annotate_page(img, by_page.get(p, []), header))
    if summary:
        size = out[0].size if out else (1700, 2200)
        exam = template.exam_name if template else submission.exam_name
        version = template.version_label if template else ""
        ordered = sorted(grades, key=lambda g: (g.region_id if g.region_id is not None else 1e9))
        out[:0] = _summary_pages(size, exam, version, student, submission, ordered)
    return out

# ---------- files ----------
def _safe(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", s or "", flags=re.UNICODE).strip("_")
    return s[:80] or "untitled"

def pdf_filename(sub: Submission, student: Optional[Student], exam: str) -> str:
    if student is None:
        return f"unassigned_{sub.id}.pdf"
    return f"{_safe(student.student_id or student.name)}_{_safe(exam)}.pdf"

def save_pdf(pages: List[Image.Image], out_path: str):
    if not pages:
        raise ValueError("No pages to export (scan images missing?)")
    dpi = max(72, round(pages[0].size[0] / LETTER_WIDTH_IN))
    pages[0].save(out_path, "PDF", save_all=True, append_images=pages[1:], resolution=dpi)

def export_submission_pdf(sub: Submission, out_path: str, summary: bool = True) -> str:
    template = load_template(sub.template_id) if sub.template_id else None
    student = next((s for s in list_students(all_courses=True) if s.id == sub.student_id), None)
    save_pdf(render_feedback_pages(sub, template, list_grades(sub.id), student, summary), out_path)
    return out_path

def export_exam(exam_name: str, out_dir: str, summary: bool = True, only_graded: bool = True,
                progress: Callable[[int, int, str], None] = None,
                course_id: int = None) -> List[Tuple[Optional[Student], str]]:
    """Write one feedback PDF per submission of the exam (of the current course, or course_id)
    plus index.csv (for LMS upload / mail merge)."""
    os.makedirs(out_dir, exist_ok=True)
    templates = {t.id: t for t in list_versions(exam_name, course_id)}
    students = {s.id: s for s in list_students(all_courses=True)}
    subs = [s for s in list_submissions(template_ids=list(templates))
            if not only_graded or s.status == SUB_GRADED]
    results, used = [], set()
    rows = []
    for i, sub in enumerate(subs):
        student = students.get(sub.student_id)
        if progress:
            progress(i, len(subs), f"Exporting {student.name if student else 'unassigned'}…")
        grades = list_grades(sub.id)
        if not grades:
            continue
        fname = pdf_filename(sub, student, exam_name)
        if fname in used:
            fname = fname[:-4] + f"_{sub.id}.pdf"
        used.add(fname)
        path = os.path.join(out_dir, fname)
        save_pdf(render_feedback_pages(sub, templates.get(sub.template_id), grades, student, summary), path)
        results.append((student, path))
        rows.append([student.name if student else (sub.name_read or ""), student.student_id if student else "",
                     student.email if student else "", _fmt(sum(g.score for g in grades)),
                     _fmt(sum(g.max_points for g in grades)), fname])
    with open(os.path.join(out_dir, "index.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "student_id", "email", "total", "max", "pdf"])
        w.writerows(rows)
    if progress:
        progress(len(subs), len(subs), f"Exported {len(results)} PDFs")
    return results
