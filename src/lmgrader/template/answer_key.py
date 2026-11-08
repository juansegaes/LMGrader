"""Answer keys written from the exam's LaTeX source (e.g. by Claude Code), imported into a version.

The professor writes the exam in LaTeX; instead of reading the printed questions back with the
vision model, a JSON answer key carries the exact LaTeX, points, expected answers and (optionally)
rubrics. Boxes still come from the page (auto-detect or drawn); the key is matched to them by label
("1a" <-> region "Q1 (a)"). The format is documented in assets/answer_key_skill/SKILL.md, which is
also the Claude Code skill that writes these files.

    {"format": "lmgrader-answer-key", "version": 2, "exam": "...", "instructions": "...",
     "questions": [{"label": "1", "stem": "Let $A=...$.",
                    "parts": [{"label": "a", "text": "...", "points": 4, "answer": "...", "type": "open",
                               "rubric": {"scoring": "positive",
                                          "items": [{"points": 2, "description": "..."}]},
                               "notes": "..."}]},
                   {"label": "2", "text": "...", "points": 2, "type": "true_false", "answer": "False"}]}

A rubric is {"max_points"?, "scoring": "positive" | "negative", "items": [{"points", "description"}]}
(positive: start at 0, items add up to the points; negative: start at the points, items are
deductions). Version 1 keys wrote the rubric as a list of {"points", "criterion"}: still read.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from ..models import TestTemplate, Rubric, RubricItem, SCORING_NEGATIVE, SCORING_POSITIVE
from .layout import parse_label

FORMAT = "lmgrader-answer-key"
TYPES = ("open", "true_false", "multiple_choice")
SKILL_PATH = Path(__file__).resolve().parent.parent / "assets" / "answer_key_skill" / "SKILL.md"

@dataclass
class KeyItem:
    label: str                 # full label, e.g. "1a" or "2"
    text: str = ""             # LaTeX of the question (only the part, for parts)
    stem: str = ""             # shared main question (parts only; same for every part)
    points: Optional[float] = None
    answer: str = ""
    type: str = "open"
    rubric: List[Tuple[float, str]] = field(default_factory=list)   # (points, description) items
    notes: str = ""
    scoring: str = SCORING_POSITIVE   # negative: start at the points, rubric items are deductions

@dataclass
class AnswerKey:
    exam: str = ""
    exam_version: str = ""
    instructions: str = ""
    items: List[KeyItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)   # things the reader couldn't do (shown at import)

@dataclass
class ImportReport:
    matched: List[str] = field(default_factory=list)          # region labels that got a key item
    no_box: List[str] = field(default_factory=list)           # key items without a region
    no_key: List[str] = field(default_factory=list)           # regions without a key item
    replaced: List[str] = field(default_factory=list)         # regions whose text/rubric changed
    rubrics_from_key: List[str] = field(default_factory=list) # rubrics set without the LLM
    notes_cleared: List[str] = field(default_factory=list)    # replace: notes removed (none in the key)
    answers_cleared: List[str] = field(default_factory=list)  # replace: answers removed (none in the key)

    def summary(self) -> str:
        lines = [f"Matched {len(self.matched)} question(s): {', '.join(self.matched) or '-'}"]
        if self.rubrics_from_key:
            lines.append(f"Rubrics in the key: {', '.join(self.rubrics_from_key)}")
        if self.notes_cleared:
            lines.append(f"Notes removed (not in the key): {', '.join(self.notes_cleared)}")
        if self.answers_cleared:
            lines.append(f"Answers removed (not in the key): {', '.join(self.answers_cleared)}")
        if self.no_box:
            lines.append(f"In the key but no box on the template: {', '.join(self.no_box)}")
        if self.no_key:
            lines.append(f"Boxes not in the key (unchanged): {', '.join(self.no_key)}")
        return "\n".join(lines)

def _num(v, where: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: points must be a number, got {v!r}")

def _part_label(qlabel: str, plabel: str) -> str:
    """'a', '(a)', 'a)', '1a', '1(a)' -> '1a'."""
    p = str(plabel).strip()
    if parse_label(p) is not None and parse_label(p)[1]:
        return p
    return f"{qlabel}{p.strip('().').strip()}"

def _item(d: dict, label: str, stem: str = "") -> KeyItem:
    where = f"question {label}"
    typ = re.sub(r"[^a-z]+", "_", str(d.get("type") or "open").strip().lower()).strip("_")
    typ = {"tf": "true_false", "t_f": "true_false", "truefalse": "true_false", "mc": "multiple_choice",
           "multiplechoice": "multiple_choice"}.get(typ, typ)
    if typ not in TYPES:
        raise ValueError(f"{where}: unknown type {d.get('type')!r} (use one of {', '.join(TYPES)})")
    raw = d.get("rubric") or []
    scoring, rub_max = SCORING_POSITIVE, None
    if isinstance(raw, dict):                 # version 2: {"scoring", "max_points", "items"}
        s = str(raw.get("scoring") or SCORING_POSITIVE).strip().lower()
        if s not in (SCORING_POSITIVE, SCORING_NEGATIVE):
            raise ValueError(f"{where}: rubric scoring must be \"positive\" or \"negative\", got {s!r}")
        scoring, rub_max = s, _num(raw.get("max_points"), f"{where} rubric max_points")
        raw = raw.get("items") or []
    if not isinstance(raw, list):
        raise ValueError(f"{where}: \"rubric\" must be an object with \"items\" (or a list of items)")
    rubric = []
    for n, c in enumerate(raw):
        if isinstance(c, str):
            m = re.match(r"\s*\[?\s*([+-]?[\d.]+)\s*(?:pts?|points?)?\s*\]?\s*[:\-]?\s*(.+)", c)
            if not m:
                raise ValueError(f"{where}: rubric item {n + 1} needs points, e.g. '[2 pt] correct setup'")
            rubric.append((float(m.group(1)), m.group(2).strip()))
            continue
        pts = _num(c.get("points"), f"{where} rubric item {n + 1}")
        desc = str(c.get("description") or c.get("criterion") or "").strip()
        if pts is None or not desc:
            raise ValueError(f"{where}: rubric item {n + 1} needs 'points' and 'description'")
        rubric.append((pts, desc))
    points = _num(d.get("points"), where)
    if points is None:
        points = rub_max
    if points is None and rubric and scoring == SCORING_POSITIVE:
        points = sum(p for p, _ in rubric if p > 0)
    return KeyItem(label=label, text=str(d.get("text") or "").strip(), stem=stem, points=points,
                   answer=str(d.get("answer") or "").strip(), type=typ, rubric=rubric,
                   notes=str(d.get("notes") or "").strip(), scoring=scoring)

def parse_answer_key(data) -> AnswerKey:
    """dict (or JSON text) -> AnswerKey. Raises ValueError with a readable message."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError("An answer key is a JSON object with a \"questions\" list.")
    if data.get("format") not in (None, FORMAT):
        raise ValueError(f"Unknown format {data.get('format')!r} (expected {FORMAT!r}).")
    key = AnswerKey(exam=str(data.get("exam") or ""), exam_version=str(data.get("exam_version") or ""),
                    instructions=str(data.get("instructions") or "").strip())
    for n, q in enumerate(data["questions"]):
        if not isinstance(q, dict) or not str(q.get("label") or "").strip():
            raise ValueError(f"Question #{n + 1} has no \"label\".")
        ql = str(q["label"]).strip()
        parts = q.get("parts") or []
        if parts:
            stem = str(q.get("stem") or q.get("text") or "").strip()
            for p in parts:
                if not str(p.get("label") or "").strip():
                    raise ValueError(f"A part of question {ql} has no \"label\".")
                key.items.append(_item(p, _part_label(ql, p["label"]), stem))
        else:
            key.items.append(_item(q, ql))
    seen = set()
    for it in key.items:
        k = match_key(it.label)
        if k in seen:
            raise ValueError(f"Label {it.label} appears twice in the key.")
        seen.add(k)
    return key

def load_answer_key(path) -> AnswerKey:
    return parse_answer_key(Path(path).read_text(encoding="utf-8"))

def match_key(label: str):
    """Labels that name the same question compare equal: 'Q1 (a)' == '1a', 'Problem 3' == '3'."""
    p = parse_label(label)
    if p is not None:
        return p
    return re.sub(r"[^a-z0-9]", "", label.lower())

def key_rubric(it: KeyItem, points: float) -> Rubric:
    """The rubric written straight from the key, or an empty Rubric when the LLM still has to
    write it. True/false and multiple choice are all-or-nothing (one item worth every point)."""
    if it.rubric:
        items = [RubricItem(p, d) for p, d in it.rubric]
        scoring = it.scoring
    elif it.type in ("true_false", "multiple_choice") and it.answer:
        items = [RubricItem(points, f"Answers {it.answer} (all or nothing: no partial credit)")]
        scoring = SCORING_POSITIVE
    else:
        return Rubric(max_points=points)
    return Rubric(max_points=points, scoring=scoring, expected_answer=it.answer, items=items)

def _same_rubric(a: Rubric, b: Rubric) -> bool:
    """Equal content (item ids are new on every import)."""
    key = lambda r: (r.scoring, r.expected_answer, [(it.points, it.description) for it in r.items])
    return key(a) == key(b)

def apply_answer_key(t: TestTemplate, key: AnswerKey, rubrics: bool = True,
                     replace: bool = False) -> ImportReport:
    """Fill the matching regions of t from the key: LaTeX question, stem, points, expected
    answer, notes and (rubrics=True, when the key has enough) the rubric. A rubric not taken from
    the key is cleared if the question changed, so "Generate rubrics" writes it from the answer.
    replace: clear every region's expected answer and notes first, so what the key leaves out
    ends up empty (re-importing an edited key); otherwise only what the key has is written."""
    rep = ImportReport()
    by_key: Dict[object, KeyItem] = {match_key(it.label): it for it in key.items}
    used = set()
    if replace:
        for r in t.regions:
            it = by_key.get(match_key(r.problem))
            if r.grading_notes.strip() and not (it and it.notes):
                rep.notes_cleared.append(r.problem)
            if r.answer_key.strip() and not (it and it.answer):
                rep.answers_cleared.append(r.problem)
            if it is None:   # matched regions are cleared below, after their rubric basis is read
                r.grading_notes, r.answer_key = "", ""
    for r in t.regions:
        k = match_key(r.problem)
        it = by_key.get(k)
        if it is None:
            rep.no_key.append(r.problem)
            continue
        used.add(k)
        rep.matched.append(r.problem)
        old_text, old_rubric = r.question_text, r.rubric.copy()
        old_basis = (r.question_text, r.points, r.answer_key)   # what a rubric was written for
        if replace:
            r.grading_notes, r.answer_key = "", ""
        if it.text:
            r.question_text = it.text
        if it.points is not None:
            r.points = it.points
            r.rubric.max_points = it.points
        if it.answer:
            r.answer_key = it.answer
        if it.notes:
            r.grading_notes = it.notes
        if it.stem and isinstance(k, tuple) and k[1]:
            t.stems[str(k[0])] = it.stem
        rub = key_rubric(it, r.points) if rubrics else Rubric(max_points=r.points)
        if rub:
            if not _same_rubric(rub, r.rubric):
                r.rubric = rub     # an unchanged rubric keeps its item ids (hand grades use them)
            rep.rubrics_from_key.append(r.problem)
        elif (r.question_text, r.points, r.answer_key) != old_basis:
            r.rubric = Rubric(max_points=r.points)    # stale: written for other text/points/answer
        if (old_text or old_rubric) and (old_text != r.question_text or not _same_rubric(old_rubric, r.rubric)):
            rep.replaced.append(r.problem)
    rep.no_box = [it.label for it in key.items if match_key(it.label) not in used]
    return rep

def describe(key: AnswerKey) -> str:
    """One line per question, for checking a key file from the command line."""
    out = [f"Exam: {key.exam or '-'}  version: {key.exam_version or '-'}  questions: {len(key.items)}"]
    if key.instructions:
        out.append(f"Instructions: {key.instructions[:80]}")
    for it in key.items:
        pts = f"{it.points:g} pt" if it.points is not None else "points not set"
        neg = it.scoring == SCORING_NEGATIVE
        rub = ((f"deductions rubric {len(it.rubric)} item(s)" if neg else
                f"rubric {len(it.rubric)} item(s), sum {sum(p for p, _ in it.rubric if p > 0):g}") if it.rubric else
               "all-or-nothing rubric" if it.type != "open" and it.answer else "rubric by LLM")
        warn = ""
        if (it.rubric and not neg and it.points is not None
                and abs(sum(p for p, _ in it.rubric if p > 0) - it.points) > 1e-6):
            warn = "  <- rubric points don't add up to the question's points"
        if it.rubric and neg and any(p > 0 for p, _ in it.rubric):
            warn = "  <- a deductions rubric should only have negative items"
        if not it.text:
            warn += "  <- no text"
        if not it.answer:
            warn += "  <- no answer"
        out.append(f"  {it.label:6} {it.type:16} {pts:15} {rub}{warn}")
    return "\n".join(out)

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python -m lmgrader.template.answer_key <exam>.lmgrader.json | <exam>.tex")
    try:
        if sys.argv[1].lower().endswith(".tex"):
            from .tex_import import load_tex
            k = load_tex(sys.argv[1])
        else:
            k = load_answer_key(sys.argv[1])
        print(describe(k))
        for w in k.warnings:
            print(f"Note: {w}")
    except (OSError, ValueError) as e:
        sys.exit(f"Invalid answer key: {e}")
