"""Text with LaTeX math -> Qt rich text: formulas drawn with matplotlib mathtext (as in the
feedback PDFs) and embedded as inline PNG images."""
import base64
import html
import io
import re
from ..export.feedback import _MATH_RE, _render_math, latex_to_text

# Formulas are drawn at the size they are shown: Qt shrinks <img> without smoothing, which
# drops thin strokes (the bars of "="). Computer Modern looks smaller than the UI font: enlarge.
MATH_SIZE = 1.2

def _text(s: str) -> str:
    return html.escape(s).replace("\n", "<br>")

def _img(tex: str, size_px: int, color: str):
    c = color.lstrip("#")
    rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    r = _render_math(tex, round(size_px * MATH_SIZE), rgb)
    if r is None:
        return None
    buf = io.BytesIO(); r[0].save(buf, "PNG")
    return f"<img src='data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}' align=middle>"

def latex_html(text: str, color: str, size_px: int = 13) -> str:
    """Rich text for a QLabel: $...$ / \\(...\\) inline, $$...$$ / \\[...\\] on their own line.
    A formula mathtext can't draw is shown as readable plain text (latex_to_text)."""
    text = text or ""
    out, pos, after_display = [], 0, False
    for m in _MATH_RE.finditer(text):
        seg = text[pos:m.start()]
        if after_display and seg.startswith("\n"):
            seg = seg[1:]          # the display formula already ended the line
        tex = next(g for g in m.groups() if g is not None)
        display = m.group(1) is not None or m.group(2) is not None
        if display and seg and not seg.endswith("\n"):
            seg += "\n"            # a display formula starts its own line
        out.append(_text(seg))
        img = _img(tex, size_px, color)
        if img is None:   # "$5 ... $" was probably money: keep it as written
            img = _text(m.group(0) if re.match(r"\d", tex) else latex_to_text(tex))
        out.append(img + ("<br>" if display else ""))
        after_display = display
        pos = m.end()
    seg = text[pos:]
    if after_display and seg.startswith("\n"):
        seg = seg[1:]
    out.append(_text(seg))
    s = re.sub(r"(<br>)+$", "", "".join(out))
    return f"<span style='color:{color}'>{s}</span>"
