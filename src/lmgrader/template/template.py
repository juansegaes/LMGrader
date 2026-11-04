"""Template management helpers."""
import copy
import json
from typing import List, Optional, Tuple
from ..models import TestTemplate, AnswerRegion
from ..db import save_template, load_template, list_templates
from .alignment import read_image
from .layout import parse_label, _part_key as part_key
from ..llm.client import repair_latex_text, is_repetitive

def create_template_from_image(name: str, image_path, exam_name: str = "",
                               version_label: str = "A") -> TestTemplate:
    """image_path: one page path or a list of page paths (multi-page exam)."""
    pages = [image_path] if isinstance(image_path, str) else list(image_path)
    for p in pages:
        if read_image(p) is None:
            raise ValueError(f"Cannot read image: {p}")
    h, w = read_image(pages[0]).shape[:2]
    return TestTemplate(name=name, image_path=pages[0], width=w, height=h, regions=[],
                        exam_name=exam_name or name, version_label=version_label,
                        page_paths=pages if len(pages) > 1 else [])

def add_region(t: TestTemplate, problem: str, group: str, rect, points=1.0, rubric="") -> AnswerRegion:
    r = AnswerRegion(problem=problem, group=group, rect=tuple(rect), points=points, rubric=rubric)
    t.regions.append(r)
    return r

def duplicate_as_version(src: TestTemplate, image_path, version_label: str, keep_text: bool = True) -> TestTemplate:
    """New version of the same exam: copies the outline (regions with labels, points, notes and
    rects; name/ID fields; scan layout; columns) onto new page image(s). keep_text also copies
    the LaTeX questions, stems and rubrics (same questions in both versions). Rects are rescaled
    if the new pages have a different size. Regions on pages the new version doesn't have are
    dropped."""
    t = create_template_from_image(f"{src.exam_name} v{version_label}", image_path,
                                   src.exam_name, version_label)
    sx = t.width / src.width if src.width else 1.0
    sy = t.height / src.height if src.height else 1.0
    scale = lambda r: (int(r[0] * sx), int(r[1] * sy), int(r[2] * sx), int(r[3] * sy)) if r else None
    for r in src.regions:
        if r.page >= t.page_count:
            continue
        nr = copy.deepcopy(r)
        nr.id = None
        nr.rect = scale(r.rect)
        nr.question_rect = scale(r.question_rect)
        if not keep_text:
            nr.question_text, nr.rubric, nr.answer_key = "", "", ""
        t.regions.append(nr)
    if src.name_page < t.page_count:
        t.name_rect, t.name_page = scale(src.name_rect), src.name_page
    if src.id_page < t.page_count:
        t.id_rect, t.id_page = scale(src.id_rect), src.id_page
    if src.version_page < t.page_count:
        t.version_rect, t.version_page = scale(src.version_rect), src.version_page
    t.columns = src.columns
    t.stems = dict(src.stems) if keep_text else {}
    if t.page_count == src.page_count:
        t.scan_layout = list(src.scan_layout)
    return t

# ---------- sub-questions ('1a', '1b' are parts of question 1, which has a shared stem) ----------
def part_of(label: str) -> Optional[Tuple[str, str]]:
    """'Q1b' -> ('1', 'b'); None for a question without parts ('Q3') or an unparsable label."""
    p = parse_label(label)
    return (str(p[0]), p[1]) if p and p[1] else None

def siblings(t: TestTemplate, i: int) -> List[int]:
    """Indices of every part of the same main question as region i (including i), in part order."""
    me = part_of(t.regions[i].problem)
    if me is None:
        return [i]
    idx = [j for j, r in enumerate(t.regions) if (part_of(r.problem) or ("", ""))[0] == me[0]]
    return sorted(idx, key=lambda j: part_key(part_of(t.regions[j].problem)[1]))

def _json_reply(text: str) -> Optional[dict]:
    """A raw {"stem": ..., "part": ...} model reply that ended up in a text box (old bug)."""
    s = (text or "").strip()
    if not (s.startswith("{") and s.endswith("}") and ('"part"' in s or '"stem"' in s)):
        return None
    try:
        d = json.loads(s)
    except ValueError:
        return None
    return d if isinstance(d, dict) and ("part" in d or "stem" in d) else None

def clean_question_text(text: str) -> str:
    """Question text without a leftover raw JSON reply (the part, else the stem)."""
    d = _json_reply(text)
    if d is None:
        return text
    return str(d.get("part") or "").strip() or str(d.get("stem") or "").strip()

def repair_part_json(t: TestTemplate) -> List[str]:
    """Clean question texts saved by earlier versions:
    - a raw {"stem", "part"} reply in a question box -> the part's text (or the stem's, when the
      model left "part" empty); a first part's stem becomes the main question if none is set;
    - LaTeX damaged by JSON decoding ('\\x08egin' -> '\\begin', tab + 'ext' -> '\\text');
    - text where the model got stuck repeating itself is cleared, so extraction fills it again.
    Returns the list of what changed ([] if nothing)."""
    changes = []
    for i, r in enumerate(t.regions):
        d = _json_reply(r.question_text)
        if d is not None:
            stem, part = str(d.get("stem") or "").strip(), str(d.get("part") or "").strip()
            if not part:
                stem, part = "", stem
            r.question_text = part
            me = part_of(r.problem)
            if stem and me and siblings(t, i)[0] == i and not t.stems.get(me[0], "").strip():
                t.stems[me[0]] = stem
            changes.append(f"{r.problem}: split a raw model reply")
        for attr in ("question_text", "rubric"):
            old = getattr(r, attr)
            new = repair_latex_text(old)
            if attr == "question_text" and is_repetitive(new):
                new = ""
                changes.append(f"{r.problem}: cleared garbled question text")
            elif new != old:
                changes.append(f"{r.problem}: repaired LaTeX")
            setattr(r, attr, new)
    for num, stem in list(t.stems.items()):
        new = repair_latex_text(stem)
        if is_repetitive(new):
            new = ""
            changes.append(f"main question {num}: cleared garbled text")
        elif new != stem:
            changes.append(f"main question {num}: repaired LaTeX")
        t.stems[num] = new
    return changes

def question_context(t: TestTemplate, i: int) -> str:
    """The question text sent to the LLM for region i. A part gets the main question's stem,
    the earlier parts (as context only) and then the part itself."""
    r = t.regions[i]
    me = part_of(r.problem)
    if me is None:
        return clean_question_text(r.question_text)
    num, letter = me
    out = []
    stem = (t.stems.get(num) or "").strip()
    if stem:
        out.append(f"Main question {num}:\n{stem}")
    sib = siblings(t, i)
    earlier = [t.regions[j] for j in sib[:sib.index(i)]]
    if earlier:
        out.append("Earlier parts (context only, do not grade them):\n" + "\n".join(
            f"({part_of(e.problem)[1]}) {clean_question_text(e.question_text).strip() or '(text not available)'}"
            for e in earlier))
    out.append(f"Part to grade: ({letter})\n{clean_question_text(r.question_text)}")
    return "\n\n".join(out)

def persist(t: TestTemplate) -> int:
    return save_template(t)

def fetch(tid: int) -> TestTemplate:
    return load_template(tid)

def fetch_all():
    return list_templates()
