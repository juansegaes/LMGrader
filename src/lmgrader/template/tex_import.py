"""Read questions (and answers, if the source has them) straight from an exam's .tex file.

No LLM: a plain parser for the two common ways exams are written:
  * the `exam` document class: \\question[pts], \\begin{parts} \\part[pts], solution environments,
    choices / checkboxes with \\CorrectChoice;
  * enumerate lists: each top-level \\item is a question, a nested enumerate holds its parts;
    points written as "(4 pts)" / "[4 points]"; answers in a solution/answer environment.
The result is an answer_key.AnswerKey, imported exactly like a JSON answer key. Layouts this
parser doesn't know are better served by the Claude Code skill (assets/answer_key_skill).
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .answer_key import AnswerKey, KeyItem

SOLUTION_ENVS = ("solution", "solutionorbox", "solutionorlines", "solutionordottedlines",
                 "solutionorgrid", "answer", "solucion", "respuesta")
CHOICE_ENVS = ("choices", "oneparchoices", "checkboxes", "oneparcheckboxes")
_TF = re.compile(r"^\W*(true\s+or\s+false|false\s+or\s+true|t\s*/\s*f\b|verdadero\s+o\s+falso|"
                 r"falso\s+o\s+verdadero|v\s*/\s*f\b)", re.I)
_POINTS = re.compile(r"[(\[]\s*(\d+(?:\.\d+)?)\s*(?:pts?|points?|puntos?|marks?)\.?\s*[)\]]", re.I)

# ---------- low-level LaTeX helpers ----------
def _strip_comments(s: str) -> str:
    out = []
    for line in s.splitlines():
        m = re.search(r"(?<!\\)%", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)

def _group(s: str, i: int, open_="{", close="}") -> Tuple[str, int]:
    """s[i] == open_: (content, index after the matching close)."""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2; continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)

def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    return i

def _opt(s: str, i: int) -> Tuple[Optional[str], int]:
    """Optional [arg] at i (after spaces)."""
    j = _skip_ws(s, i)
    if j < len(s) and s[j] == "[":
        return _group(s, j, "[", "]")
    return None, i

def _env_spans(s: str, name: str) -> List[Tuple[int, int, int, int]]:
    """Top-level \\begin{name}...\\end{name}: (begin, content start, content end, after end)."""
    tok = re.compile(r"\\(begin|end)\{" + re.escape(name) + r"\}")
    spans, depth, start, cstart = [], 0, 0, 0
    for m in tok.finditer(s):
        if m.group(1) == "begin":
            if depth == 0:
                start, cstart = m.start(), m.end()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                spans.append((start, cstart, m.start(), m.end()))
    return spans

def _split_top(s: str, cmd: str) -> List[Tuple[int, int]]:
    """Positions of \\cmd at environment depth 0 of s: [(start, end of the command name)]."""
    tok = re.compile(r"\\(begin|end)\{[^}]*\}|\\" + cmd + r"(?![A-Za-z])")
    out, depth = [], 0
    for m in tok.finditer(s):
        if m.group(1) == "begin":
            depth += 1
        elif m.group(1) == "end":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append((m.start(), m.end()))
    return out

# ---------- macros and \input ----------
def _inline_inputs(text: str, base: Path, depth: int = 0) -> str:
    if depth > 5:
        return text
    def sub(m):
        p = base / m.group(2).strip()
        if not p.suffix:
            p = p.with_suffix(".tex")
        try:
            return _inline_inputs(_strip_comments(p.read_text(encoding="utf-8", errors="replace")),
                                  p.parent, depth + 1)
        except OSError:
            return ""
    return re.sub(r"\\(input|include)\s*\{([^}]*)\}", sub, text)

def _macros(preamble: str) -> Dict[str, Tuple[int, str]]:
    """\\newcommand / \\renewcommand / \\def / \\DeclareMathOperator -> {name: (n args, body)}."""
    out: Dict[str, Tuple[int, str]] = {}
    for m in re.finditer(r"\\(?:re|provide)?newcommand\*?\s*\{?\s*\\([A-Za-z]+)\s*\}?", preamble):
        n, i = _opt(preamble, m.end())
        _, i = _opt(preamble, i)                 # default value of an optional argument: ignored
        i = _skip_ws(preamble, i)
        if i < len(preamble) and preamble[i] == "{":
            body, _ = _group(preamble, i)
            out[m.group(1)] = (int(n) if n and n.strip().isdigit() else 0, body)
    for m in re.finditer(r"\\def\s*\\([A-Za-z]+)\s*\{", preamble):
        out[m.group(1)] = (0, _group(preamble, m.end() - 1)[0])
    for m in re.finditer(r"\\DeclareMathOperator\*?\s*\{\s*\\([A-Za-z]+)\s*\}\s*\{", preamble):
        out[m.group(1)] = (0, r"\operatorname{" + _group(preamble, m.end() - 1)[0] + "}")
    return out

STRUCTURE = {"question", "titledquestion", "bonusquestion", "part", "item", "choice", "CorrectChoice",
             "begin", "end"}

def _expand(s: str, macros: Dict[str, Tuple[int, str]]) -> str:
    """Expand the author's macros so the text makes sense without the preamble. Commands that
    mark the structure (\\question, \\part, \\item, ...) are never expanded."""
    macros = {k: v for k, v in macros.items() if k not in STRUCTURE}
    for _ in range(4):                           # macros using macros
        changed = False
        for name, (n, body) in macros.items():
            rx = re.compile(r"\\" + name + r"(?![A-Za-z])")
            pos, out = 0, []
            for m in rx.finditer(s):
                if m.start() < pos:
                    continue
                i, args = m.end(), []
                for _ in range(n):
                    j = _skip_ws(s, i)
                    if j < len(s) and s[j] == "{":
                        a, i = _group(s, j)
                    else:
                        a, i = (s[j], j + 1) if j < len(s) else ("", j)
                    args.append(a)
                rep = body
                for k, a in enumerate(args, 1):
                    rep = rep.replace(f"#{k}", a)
                out.append(s[pos:m.start()]); out.append(rep)
                pos, changed = i, True
            out.append(s[pos:])
            s = "".join(out)
        if not changed:
            break
    return s

# ---------- cleaning question text ----------
_DROP_ARGS = ("vspace", "vspace*", "hspace", "hspace*", "makeemptybox", "pointformat", "label")
_DROP = ("vfill", "newpage", "clearpage", "pagebreak", "noindent", "bigskip", "medskip", "smallskip",
         "hfill", "centering", "droppoints", "droppoints*", "droptotalpoints", "fillin", "answerline",
         "addpoints", "par", "newline", "linebreak", "null")

def clean_text(s: str) -> str:
    """Question LaTeX without layout: answer space, page breaks and spacing go; figures become
    "[Figure]" (the grader sees the printed page anyway)."""
    s = re.sub(r"\\begin\{(tikzpicture|figure)\}.*?\\end\{\1\}", "[Figure]", s, flags=re.S)
    s = re.sub(r"\\includegraphics\s*(\[[^\]]*\])?\s*\{[^}]*\}", "[Figure]", s)
    s = re.sub(r"\\(begin|end)\{center\}", "", s)
    for cmd in _DROP_ARGS:
        s = re.sub(r"\\" + re.escape(cmd) + r"(?![A-Za-z])\s*(\[[^\]]*\])?\s*\{[^{}]*\}", "", s)
    for cmd in _DROP:
        s = re.sub(r"\\" + re.escape(cmd) + r"(?![A-Za-z*])\s*(\[[^\]]*\])*", "", s)
    s = re.sub(r"\\\\\s*(\[[^\]]*\])?\s*$", "", s, flags=re.M)     # trailing line breaks
    lines = [ln.strip() for ln in s.splitlines()]
    s = re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()     # removed layout leaves blank lines
    return s

def _take_points(s: str) -> Tuple[Optional[float], str]:
    m = _POINTS.search(s)
    if not m:
        return None, s
    return float(m.group(1)), (s[:m.start()] + s[m.end():]).strip()

def _take_solution(s: str) -> Tuple[str, str]:
    """Remove solution environments from s; return (answer, rest)."""
    answer = []
    for env in SOLUTION_ENVS:
        for b, cs, ce, e in reversed(_env_spans(s, env)):
            body = s[cs:ce]
            _, k = _opt(body, 0)                 # solutionorbox[2in]
            answer.insert(0, clean_text(body[k:]))
            s = s[:b] + s[e:]
    return "\n".join(a for a in answer if a), s

def _take_choices(s: str) -> Tuple[str, str]:
    """Choice environments -> '(A) ... (B) ...' in the text; return (correct letters, text)."""
    correct = []
    for env in CHOICE_ENVS:
        for b, cs, ce, e in reversed(_env_spans(s, env)):
            body = s[cs:ce]
            marks = [(m.start(), m.end(), m.group(1) == "CorrectChoice")
                     for m in re.finditer(r"\\(choice|CorrectChoice)(?![A-Za-z])", body)]
            opts = []
            for n, (st, en, ok) in enumerate(marks):
                end = marks[n + 1][0] if n + 1 < len(marks) else len(body)
                letter = chr(65 + n)
                opts.append(f"({letter}) {clean_text(body[en:end])}")
                if ok:
                    correct.append(f"({letter})")
            s = s[:b] + "\n" + "\n".join(opts) + "\n" + s[e:]
    return ", ".join(correct), s

def _item(label: str, raw: str, points: Optional[float], stem: str = "") -> KeyItem:
    answer, raw = _take_solution(raw)
    mc, raw = _take_choices(raw)
    p_text, raw = _take_points(raw)
    text = clean_text(raw)
    typ = "multiple_choice" if mc else "true_false" if _TF.search(text) else "open"
    if typ == "true_false" and answer:
        m = re.match(r"\W*(true|false|verdadero|falso|v|f)\b", answer, re.I)
        if m:
            word = m.group(1).lower()
            answer = ("True" if word in ("true", "verdadero", "v") else "False") + answer[m.end():]
    return KeyItem(label=label, text=text, stem=stem, points=points if points is not None else p_text,
                   answer=answer or mc, type=typ)

def _num(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v is not None and v.strip() else None
    except ValueError:
        return None

# ---------- the two layouts ----------
def _parse_exam_class(body: str, warnings: List[str]) -> List[KeyItem]:
    items = []
    for env in ("questions",):
        spans = _env_spans(body, env)
        if spans:
            body = "\n".join(body[cs:ce] for _, cs, ce, _ in spans)
    qs = [(m.start(), m.end()) for m in re.finditer(r"\\(?:question|titledquestion|bonusquestion)(?![A-Za-z])", body)]
    for n, (st, en) in enumerate(qs, 1):
        chunk = body[en:qs[n][0] if n < len(qs) else len(body)]
        if body[st:en].endswith("titledquestion"):     # \titledquestion{Title}[pts]
            j = _skip_ws(chunk, 0)
            if j < len(chunk) and chunk[j] == "{":
                chunk = chunk[_group(chunk, j)[1]:]
        pts, i = _opt(chunk, 0)
        chunk = chunk[i:]
        parts = _env_spans(chunk, "parts")
        if not parts:
            items.append(_item(str(n), chunk, _num(pts)))
            continue
        first = len(items)
        b, cs, ce, e = parts[0]
        stem = clean_text(_take_points(_take_solution(chunk[:b])[1])[1])
        inner = chunk[cs:ce]
        if _env_spans(inner, "subparts"):
            warnings.append(f"Question {n}: subparts are kept inside their part's text.")
        marks = _split_top(inner, "part")
        for k, (pst, pen) in enumerate(marks):
            raw = inner[pen:marks[k + 1][0] if k + 1 < len(marks) else len(inner)]
            ppts, j = _opt(raw, 0)
            items.append(_item(f"{n}{chr(97 + k)}", raw[j:], _num(ppts), stem))
        if _num(pts) and not any(it.points for it in items[first:]):
            warnings.append(f"Question {n}: {_num(pts):g} points for the whole question; "
                            "set the points of each part in LMGrader.")
    return items

def _label_from(opt: Optional[str], default: str) -> str:
    if opt is None:
        return default
    lab = re.sub(r"[^0-9A-Za-z]", "", opt)
    return lab or default

def _parse_enumerate(body: str, warnings: List[str]) -> List[KeyItem]:
    spans = _env_spans(body, "enumerate")
    if not spans:
        return []
    b, cs, ce, e = max(spans, key=lambda sp: len(_split_top(body[sp[1]:sp[2]], "item")))
    inner = body[cs:ce]
    _, k0 = _opt(inner, 0)                       # enumerate options, e.g. [label=\arabic*.]
    marks = _split_top(inner, "item")
    items = []
    for n, (st, en) in enumerate(marks, 1):
        raw = inner[en:marks[n][0] if n < len(marks) else len(inner)]
        lab, i = _opt(raw, 0)
        label = _label_from(lab, str(n))
        raw = raw[i:]
        sub = _env_spans(raw, "enumerate")
        if not sub:
            items.append(_item(label, raw, None))
            continue
        sb, scs, sce, se = sub[0]
        stem_pts, stem_raw = _take_points(_take_solution(raw[:sb])[1])
        stem = clean_text(stem_raw)
        pinner = raw[scs:sce]
        _, k = _opt(pinner, 0)
        pmarks = _split_top(pinner, "item")
        for j, (pst, pen) in enumerate(pmarks):
            praw = pinner[pen:pmarks[j + 1][0] if j + 1 < len(pmarks) else len(pinner)]
            plab, pi = _opt(praw, 0)
            letter = _label_from(plab, chr(97 + j)).lower()
            letter = re.sub(r"^\d+", "", letter) or chr(97 + j)
            items.append(_item(f"{label}{letter}", praw[pi:], None, stem))
        if stem_pts is not None and not any(it.points for it in items if it.label.startswith(label)):
            warnings.append(f"Question {label}: {stem_pts:g} points for the whole question; "
                            "set the points of each part in LMGrader.")
    return items

def parse_tex(text: str, base: Optional[Path] = None) -> AnswerKey:
    """LaTeX source -> AnswerKey (questions, stems, points; answers when the source has them).
    Raises ValueError when no questions are found."""
    text = _strip_comments(text)
    if base is not None:
        text = _inline_inputs(text, base)
    m = re.search(r"\\begin\{document\}(.*?)(\\end\{document\}|$)", text, re.S)
    preamble, body = (text[:m.start()], m.group(1)) if m else ("", text)
    body = _expand(body, _macros(preamble + "\n" + body))
    title = re.search(r"\\title\s*\{", preamble + body)
    exam = clean_text(_group(preamble + body, title.end() - 1)[0]) if title else ""
    warnings: List[str] = []
    if re.search(r"\\(question|titledquestion)(?![A-Za-z])", body):
        items = _parse_exam_class(body, warnings)
    else:
        items = _parse_enumerate(body, warnings)
    if not items:
        raise ValueError("No questions found. Supported: the exam class (\\question, \\part) and "
                         "enumerate lists (\\item, nested enumerate for parts). For other layouts, "
                         "use the Claude Code answer key (How to make one).")
    if not any(it.answer for it in items):
        warnings.append("No solutions in the source: only the questions are imported "
                        "(rubrics are then written by the LLM, which solves each question).")
    return AnswerKey(exam=exam.replace("\n", " "), items=items, warnings=warnings)

def load_tex(path) -> AnswerKey:
    p = Path(path)
    return parse_tex(p.read_text(encoding="utf-8", errors="replace"), p.parent)
