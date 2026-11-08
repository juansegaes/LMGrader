"""Data models."""
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple, Optional

Rect = Tuple[int, int, int, int]  # (x, y, w, h) in template pixel coords
BLANK = -1   # scan_layout entry: a scanned page that is not a template page (back side / scratch)

def double_sided_layout(page_count: int) -> List[int]:
    """Every template page followed by its (blank) back side: 4 pages -> 8 scanned pages."""
    return [x for p in range(page_count) for x in (p, BLANK)]

def new_uid() -> str:
    return uuid.uuid4().hex[:12]

# ---------- structured rubrics ----------
SCORING_POSITIVE = "positive"   # score starts at 0; checked items add their points
SCORING_NEGATIVE = "negative"   # score starts at max points; checked items take points off

def fmt_points(p: float) -> str:
    """+2, -0.5, +0 (ASCII minus: the PDF fonts may lack U+2212)."""
    return f"{p:+g}"

@dataclass
class RubricItem:
    points: float = 0.0          # added to the score when the item is checked (may be negative)
    description: str = ""        # what the grader looks for (LaTeX allowed)
    id: str = field(default_factory=new_uid)   # stable: hand grades store the ids they applied

@dataclass
class Rubric:
    """A question's rubric, stored in the template as JSON:
        {"max_points": 4, "scoring": "positive", "expected_answer": "...",
         "items": [{"id": "...", "points": 2, "description": "Correct setup"}, ...]}
    positive: the score starts at 0 and every checked item adds its points;
    negative: it starts at max_points and every checked item adds its (negative) points.
    Items may carry either sign in both modes; the score is always clamped to 0..max_points.
    max_points mirrors the question's points (AnswerRegion.points wins when they differ)."""
    max_points: float = 0.0
    scoring: str = SCORING_POSITIVE
    expected_answer: str = ""
    items: List[RubricItem] = field(default_factory=list)

    def __bool__(self) -> bool:        # an empty rubric is falsy, like an empty list
        return not self.is_empty()

    def is_empty(self) -> bool:
        return not self.items and not self.expected_answer.strip()

    @property
    def negative(self) -> bool:
        return self.scoring == SCORING_NEGATIVE

    def item(self, item_id: str) -> Optional[RubricItem]:
        return next((it for it in self.items if it.id == item_id), None)

    def checked(self, item_ids: Iterable[str]) -> List[RubricItem]:
        """The items of `item_ids` that still exist, in rubric order."""
        ids = set(item_ids or ())
        return [it for it in self.items if it.id in ids]

    def score(self, item_ids: Iterable[str], max_points: Optional[float] = None) -> float:
        mx = self.max_points if max_points is None else max_points
        start = mx if self.negative else 0.0
        return max(0.0, min(mx, start + sum(it.points for it in self.checked(item_ids))))

    def feedback(self, item_ids: Iterable[str], comment: str = "") -> str:
        """Student-facing feedback of a hand grade: the applied items, then the comment."""
        lines = [f"{fmt_points(it.points)}  {it.description.strip()}" for it in self.checked(item_ids)]
        if comment.strip():
            lines.append(comment.strip())
        return "\n".join(lines)

    def as_text(self, max_points: Optional[float] = None) -> str:
        """The rubric as the LLM (and the Review page) reads it; "" for an empty rubric."""
        if self.is_empty():
            return ""
        mx = self.max_points if max_points is None else max_points
        lines = []
        if self.expected_answer.strip():
            lines += [f"Expected answer: {self.expected_answer.strip()}", ""]
        if self.negative:
            lines.append(f"Scoring: start from {mx:g} points and subtract every deduction that applies "
                         "(never below 0).")
            lines.append("Deductions:")
        else:
            lines.append("Criteria:")
        for it in self.items:
            pts = f"{it.points:g}" if (it.points >= 0 and not self.negative) else fmt_points(it.points)
            lines.append(f"- [{pts} pt] {it.description.strip()}")
        positive_sum = sum(it.points for it in self.items if it.points > 0)
        if not self.negative and self.items and abs(positive_sum - mx) > 1e-6:
            lines.append(f"(Note: criteria sum to {positive_sum:g}, cap the score at {mx:g}.)")
        return "\n".join(lines)

    def to_dict(self, max_points: Optional[float] = None) -> dict:
        return {"max_points": self.max_points if max_points is None else max_points,
                "scoring": self.scoring, "expected_answer": self.expected_answer,
                "items": [{"id": it.id, "points": it.points, "description": it.description}
                          for it in self.items]}

    def copy(self) -> "Rubric":
        return Rubric(self.max_points, self.scoring, self.expected_answer,
                      [RubricItem(it.points, it.description, it.id) for it in self.items])

    @classmethod
    def from_obj(cls, v, max_points: float = 0.0) -> "Rubric":
        """A stored rubric: the JSON dict, a list of items, legacy text, a Rubric, or None."""
        if isinstance(v, Rubric):
            r = v.copy()
            r.max_points = max_points or r.max_points
            return r
        if isinstance(v, str):
            return parse_rubric_text(v, max_points)
        if isinstance(v, list):
            v = {"items": v}
        if not isinstance(v, dict):
            return cls(max_points=max_points)
        items = []
        for d in v.get("items") or []:
            if not isinstance(d, dict):
                continue
            try:
                pts = float(d.get("points") or 0)
            except (TypeError, ValueError):
                pts = 0.0
            items.append(RubricItem(pts, str(d.get("description") or d.get("criterion") or ""),
                                    str(d.get("id") or "") or new_uid()))
        scoring = SCORING_NEGATIVE if v.get("scoring") == SCORING_NEGATIVE else SCORING_POSITIVE
        try:
            mx = float(max_points or v.get("max_points") or 0)
        except (TypeError, ValueError):
            mx = 0.0
        return cls(mx, scoring, str(v.get("expected_answer") or ""), items)

_POINTS = r"([+\-−]?\s*\d+(?:[.,]\d+)?)"
_ITEM_RES = [
    re.compile(rf"^\s*[-*•]?\s*\[\s*{_POINTS}\s*(?:pts?|points?)?\s*\]\s*[:\-–]?\s*(.+)$", re.I),   # - [2 pt] x
    re.compile(rf"^\s*[-*•]?\s*\(\s*{_POINTS}\s*(?:pts?|points?)?\s*\)\s*[:\-–]?\s*(.+)$", re.I),   # (2) x
    re.compile(rf"^\s*[-*•]?\s*{_POINTS}\s*(?:pts?|points?)\s*[:\-–]?\s*(.+)$", re.I),             # 2 pts: x
]

def _num(s: str) -> float:
    return float(s.replace("−", "-").replace(",", ".").replace(" ", ""))

def parse_rubric_text(text: str, max_points: float = 0.0) -> Rubric:
    """Turn a rubric written as text (by older LMGrader versions or a professor) into items:
    'Expected answer: ...' becomes expected_answer; '- [2 pt] ...', '(2) ...' and '2 pts: ...'
    lines become items; other lines continue the item above. Text without any point lines
    becomes one item worth all the points."""
    r = Rubric(max_points=max_points)
    loose, in_answer = [], False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            in_answer = False
            continue
        m = re.match(r"(?i)^expected answer\s*:\s*(.*)$", line)
        if m:
            r.expected_answer, in_answer = m.group(1).strip(), True
            continue
        if re.match(r"(?i)^(criteria|deductions|rubric)\s*:?$", line) or re.match(r"(?i)^\(note:", line):
            in_answer = False
            continue
        hit = next((m for rx in _ITEM_RES if (m := rx.match(line))), None)
        if hit:
            in_answer = False
            r.items.append(RubricItem(_num(hit.group(1)), hit.group(2).strip()))
        elif in_answer:
            r.expected_answer += "\n" + line
        elif r.items:
            r.items[-1].description += " " + line
        else:
            loose.append(line)
    if loose and not r.items:
        r.items.append(RubricItem(max_points, "\n".join(loose)))
    elif loose:
        r.expected_answer = "\n".join(filter(None, [r.expected_answer, *loose]))
    if r.items and all(it.points <= 0 for it in r.items) and any(it.points < 0 for it in r.items):
        r.scoring = SCORING_NEGATIVE        # only deductions: they are taken off the max
    return r

@dataclass
class Student:
    id: Optional[int] = None
    name: str = ""
    student_id: str = ""
    email: str = ""
    course_id: Optional[int] = None   # its course's roster (None: the current course when saved)

@dataclass
class AnswerRegion:
    """A blank answer space defined on the template."""
    id: Optional[int] = None
    problem: str = ""          # problem number/label
    group: str = ""            # problem group/section
    rect: Rect = (0, 0, 0, 0)  # answer box
    points: float = 1.0        # max points (config.DEFAULT_POINTS; the professor changes it)
    rubric: Rubric = field(default_factory=Rubric)   # structured rubric (text is converted)
    question_rect: Optional[Rect] = None  # where the printed question is
    question_text: str = ""    # fixed LaTeX transcription of the question
    page: int = 0              # template page index the region is on
    grading_notes: str = ""    # professor's notes for this question (added to the grading prompt)
    answer_key: str = ""       # professor's expected answer (from an imported answer key)
    # stable identity: grades follow it when regions are reordered (grades.region_id is the index)
    uid: str = field(default_factory=new_uid)

    def __setattr__(self, name, value):
        # a rubric given as text / JSON dict / item list (older code, custom LLM clients, answer
        # keys) is stored structured
        if name == "rubric" and not isinstance(value, Rubric):
            value = Rubric.from_obj(value, getattr(self, "points", 0.0) or 0.0)
        super().__setattr__(name, value)

    def set_rubric(self, value):
        """Assign a rubric given as a Rubric, its JSON dict, a list of items or text."""
        self.rubric = Rubric.from_obj(value, self.points)

@dataclass
class TestTemplate:
    """One version of an exam: one or more blank pages with their regions."""
    id: Optional[int] = None
    name: str = ""
    image_path: str = ""       # page 0 (kept for compatibility)
    width: int = 0             # page 0 size
    height: int = 0
    regions: List[AnswerRegion] = field(default_factory=list)
    exam_name: str = ""
    version_label: str = "A"
    name_rect: Optional[Rect] = None  # where students write their name
    name_page: int = 0
    page_paths: List[str] = field(default_factory=list)  # all pages; [] means [image_path]
    # Order of the pages in a scanned test: template page index, or BLANK for a page that is not
    # part of the template (e.g. the back side when scanning double-sided). [] = pages in order.
    scan_layout: List[int] = field(default_factory=list)
    columns: int = 0   # questions laid out in N columns (0 = let the LLM figure it out)
    id_rect: Optional[Rect] = None    # where students write their student ID (optional)
    id_page: int = 0
    # where the exam states its version ("Version A", a circled letter...); read to pick the version
    version_rect: Optional[Rect] = None
    version_page: int = 0
    # shared text (LaTeX) of questions with parts, by question number: {"1": stem of 1a, 1b, ...}
    stems: Dict[str, str] = field(default_factory=dict)
    # the course the exam belongs to (exam names are unique within a course; None: the current
    # course when saved)
    course_id: Optional[int] = None

    @property
    def pages(self) -> List[str]:
        return self.page_paths or ([self.image_path] if self.image_path else [])

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def layout(self) -> List[int]:
        """Scan layout, valid for the current page count."""
        lay = [p for p in self.scan_layout if p == BLANK or 0 <= p < self.page_count]
        if sorted(p for p in lay if p != BLANK) != list(range(self.page_count)):
            return list(range(self.page_count))
        return lay

    @property
    def scan_page_count(self) -> int:
        """Pages per scanned test, including blank (back) pages."""
        return len(self.layout)

    @property
    def blank_count(self) -> int:
        return sum(1 for p in self.layout if p == BLANK)

    @property
    def display_name(self) -> str:
        return f"{self.exam_name or self.name} — v{self.version_label}"

# Submission lifecycle (persisted, so a crash never loses progress):
#   new -> (identify) -> identified -> (human confirms student) -> ready -> grading -> graded
#   any step may end in "error"; grading resumes per region.
SUB_NEW, SUB_IDENTIFIED, SUB_READY, SUB_GRADING, SUB_GRADED, SUB_ERROR = (
    "new", "identified", "ready", "grading", "graded", "error")

@dataclass
class Submission:
    id: Optional[int] = None
    student_id: Optional[int] = None
    template_id: Optional[int] = None
    scan_front_path: str = ""   # first scanned page
    scan_back_path: str = ""    # first back page (legacy single-page)
    aligned_front_path: str = ""
    has_work_on_back: bool = False
    back_reviewed: bool = False
    name_read: str = ""          # name as transcribed by the LLM
    name_match_score: float = 0.0
    id_read: str = ""            # student ID as transcribed by the LLM
    exam_name: str = ""
    status: str = SUB_NEW
    scan_pages: List[str] = field(default_factory=list)     # one per template page
    back_pages: List[str] = field(default_factory=list)     # back of each page ("" if none)
    aligned_pages: List[str] = field(default_factory=list)
    name_confirmed: bool = False
    error: str = ""
    source: str = ""             # file the pages were cut from
    created_at: str = ""
    version_read: str = ""       # how the version was chosen, e.g. "field: B" or "layout"
    version_confirmed: bool = False   # picked by a human: re-reading never changes it
    course_id: Optional[int] = None   # course of its exam (None: the current course when saved)

    @property
    def pages(self) -> List[str]:
        return self.scan_pages or ([self.scan_front_path] if self.scan_front_path else [])

    @property
    def backs(self) -> List[str]:
        return self.back_pages or ([self.scan_back_path] if self.scan_back_path else [])

@dataclass
class Grade:
    id: Optional[int] = None
    submission_id: Optional[int] = None
    region_id: Optional[int] = None
    problem: str = ""
    group: str = ""
    score: float = 0.0
    max_points: float = 0.0
    feedback: str = ""
    needs_human_review: bool = False
    review_reason: str = ""
    extracted_image_path: str = ""
    transcription: str = ""      # what the model read in the answer box
    confidence: float = 1.0      # model's self-reported confidence (0-1)
    human_graded: bool = False   # a human set/confirmed this score
    next_part_notes: str = ""    # grader's note for the later parts of the same question
    source: str = ""             # "hand": graded in the Hand Grade tab ("" = AI, maybe reviewed)
    rubric_items: List[str] = field(default_factory=list)   # hand grade: ids of the applied items
    comment: str = ""            # hand grade: the grader's own comment (also in feedback)
    hand_group_id: Optional[int] = None   # hand grade: the answer group it was graded with

def _rect_or_none(v) -> Optional[Rect]:
    return tuple(int(x) for x in v) if v else None

def region_to_dict(r: AnswerRegion) -> dict:
    return {"uid": r.uid, "problem": r.problem, "group": r.group, "rect": list(r.rect),
            "points": r.points, "rubric": r.rubric.to_dict(r.points),
            "question_rect": list(r.question_rect) if r.question_rect else None,
            "question_text": r.question_text, "page": r.page, "grading_notes": r.grading_notes,
            "answer_key": r.answer_key}

def region_from_dict(d: dict) -> AnswerRegion:
    points = d.get("points", 0.0)
    return AnswerRegion(id=d.get("id"), problem=d.get("problem", ""),
                        group=d.get("group", ""), rect=tuple(d.get("rect", (0, 0, 0, 0))),
                        points=points, rubric=Rubric.from_obj(d.get("rubric"), points or 0.0),
                        question_rect=_rect_or_none(d.get("question_rect")),
                        question_text=d.get("question_text", ""), page=int(d.get("page", 0) or 0),
                        grading_notes=d.get("grading_notes", ""), answer_key=d.get("answer_key", ""),
                        uid=str(d.get("uid") or "") or new_uid())
