"""Data models."""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

Rect = Tuple[int, int, int, int]  # (x, y, w, h) in template pixel coords
BLANK = -1   # scan_layout entry: a scanned page that is not a template page (back side / scratch)

def double_sided_layout(page_count: int) -> List[int]:
    """Every template page followed by its (blank) back side: 4 pages -> 8 scanned pages."""
    return [x for p in range(page_count) for x in (p, BLANK)]

@dataclass
class Student:
    id: Optional[int] = None
    name: str = ""
    student_id: str = ""
    email: str = ""

@dataclass
class AnswerRegion:
    """A blank answer space defined on the template."""
    id: Optional[int] = None
    problem: str = ""          # problem number/label
    group: str = ""            # problem group/section
    rect: Rect = (0, 0, 0, 0)  # answer box
    points: float = 1.0        # max points (config.DEFAULT_POINTS; the professor changes it)
    rubric: str = ""           # fixed rubric (typed or LLM-generated once)
    question_rect: Optional[Rect] = None  # where the printed question is
    question_text: str = ""    # fixed LaTeX transcription of the question
    page: int = 0              # template page index the region is on
    grading_notes: str = ""    # professor's notes for this question (added to the grading prompt)
    answer_key: str = ""       # professor's expected answer (from an imported answer key)

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

def _rect_or_none(v) -> Optional[Rect]:
    return tuple(int(x) for x in v) if v else None

def region_to_dict(r: AnswerRegion) -> dict:
    return {"problem": r.problem, "group": r.group, "rect": list(r.rect),
            "points": r.points, "rubric": r.rubric,
            "question_rect": list(r.question_rect) if r.question_rect else None,
            "question_text": r.question_text, "page": r.page, "grading_notes": r.grading_notes,
            "answer_key": r.answer_key}

def region_from_dict(d: dict) -> AnswerRegion:
    return AnswerRegion(id=d.get("id"), problem=d.get("problem", ""),
                        group=d.get("group", ""), rect=tuple(d.get("rect", (0, 0, 0, 0))),
                        points=d.get("points", 0.0), rubric=d.get("rubric", ""),
                        question_rect=_rect_or_none(d.get("question_rect")),
                        question_text=d.get("question_text", ""), page=int(d.get("page", 0) or 0),
                        grading_notes=d.get("grading_notes", ""), answer_key=d.get("answer_key", ""))
