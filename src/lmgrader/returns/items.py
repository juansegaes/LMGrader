"""What gets returned: one item per graded submission, with its feedback PDF."""
import os
from dataclasses import dataclass
from typing import List, Optional
from ..config import data_dir
from ..db import list_versions, list_submissions, list_grades, list_students, last_return
from ..models import Submission, Student, SUB_GRADED
from ..export.feedback import export_submission_pdf, pdf_filename, _safe

@dataclass
class ReturnItem:
    sub: Submission
    student: Optional[Student]
    exam: str
    version: str
    total: float
    max: float
    needs_review: bool
    pdf_path: str = ""

    @property
    def email(self) -> str:
        return (self.student.email or "").strip() if self.student else ""

    @property
    def percent(self) -> float:
        return round(100.0 * self.total / self.max, 1) if self.max else 0.0

    def last(self, channel: str = None) -> Optional[dict]:
        return last_return(self.sub.id, channel)

def split_name(name: str):
    """'Smith, John' or 'John Smith' -> ('John', 'Smith')."""
    name = (name or "").strip()
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        return first, last
    parts = name.split()
    if len(parts) <= 1:
        return name, ""
    return " ".join(parts[:-1]), parts[-1]

def collect_items(exam_name: str, include_needs_review: bool = True) -> List[ReturnItem]:
    """Fully graded submissions of an exam (all versions)."""
    versions = {t.id: t for t in list_versions(exam_name)}
    students = {s.id: s for s in list_students()}
    items = []
    for sub in list_submissions(template_ids=list(versions)):
        if sub.status != SUB_GRADED:
            continue
        grades = list_grades(sub.id)
        if not grades:
            continue
        needs = any(g.needs_human_review for g in grades) or (sub.has_work_on_back and not sub.back_reviewed)
        if needs and not include_needs_review:
            continue
        t = versions.get(sub.template_id)
        items.append(ReturnItem(sub=sub, student=students.get(sub.student_id), exam=exam_name,
                                version=t.version_label if t else "", total=sum(g.score for g in grades),
                                max=sum(g.max_points for g in grades), needs_review=needs))
    return items

def pdf_dir(exam_name: str) -> str:
    d = data_dir() / "returns" / _safe(exam_name)
    d.mkdir(parents=True, exist_ok=True)
    return str(d)

def pdf_name(item: ReturnItem) -> str:
    """Attachment file name (unique per submission: re-takes, duplicate names)."""
    name = pdf_filename(item.sub, item.student, item.exam)
    return name[:-4] + f"_{item.sub.id}.pdf" if item.student is not None else name

def ensure_pdf(item: ReturnItem) -> str:
    """(Re)generate the feedback PDF right before it is sent/exported, so late score or feedback
    edits are always included."""
    path = os.path.join(pdf_dir(item.exam), pdf_name(item))
    export_submission_pdf(item.sub, path)
    item.pdf_path = path
    return path
