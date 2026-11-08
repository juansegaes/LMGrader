"""Hand grading: one question of one exam version across every test.

Flow per question (template version + region):
  0. the human picks the question's category (grading.autogroup): blank answers only, short
     answer or multiple choice; the last two get suggested groups the human checks; changing
     the category later, or regrading it from scratch, starts the question over (start_over);
  1. optional blank sorting: the CNN (grading.blank_cnn) proposes the blank answers, the human
     removes the ones that aren't blank, the rest join the "Blank" group (every question has
     one, possibly empty, so missed blanks can be added later);
  2. grouping: the human puts identical answers in groups; a dissolved group's answers lose
     their hand grades (dissolve_group);
  3. confirming: every group is shown with all its answers; the ones that don't belong leave it;
  4. grading: every group is graded once (its rubric items + comment apply to every member),
     then each ungrouped answer on its own.
Every hand grade is a normal `grades` row with human_graded=1 and source='hand', so Review,
feedback PDFs and returns use it as is, and the LLM never grades that question again.
Only single columns of `submissions` are written: the LLM Grade tab may be saving the same test.
"""
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from ..config import data_dir, CROP_PADDING
from ..db import (list_submissions, get_grade, save_grade, delete_grade, region_grades, list_hand_groups,
                  update_hand_group, set_aligned_page, list_grades, add_hand_group, delete_hand_group,
                  set_hand_stage, set_hand_group_members)
from ..models import Grade, Submission, TestTemplate, SUB_NEW
from ..template.alignment import read_image, align_with_score, crop_region, save_image
from .session import refresh_status

BLANK_LABEL = "Blank"

@dataclass
class Answer:
    """One test's answer to the question being hand graded."""
    sub: Submission
    crop: str = ""                         # crop of the answer box ('' = page missing from the scan)
    p_not_blank: Optional[float] = None    # CNN output, when blanks were detected

def eligible_submissions(template: TestTemplate) -> List[Submission]:
    """Tests of this version that can be graded: every one whose version is known (not still
    waiting to be read); the student may still be unconfirmed (grading is anonymous)."""
    return [s for s in list_submissions(template_id=template.id) if s.status != SUB_NEW]

def _crop_file(sub_id: int, template: TestTemplate, i: int) -> Path:
    r = template.regions[i]
    key = hashlib.sha1(f"{r.page}:{tuple(r.rect)}:{template.pages[min(r.page, template.page_count - 1)]}"
                       .encode()).hexdigest()[:8]     # a moved box gets a new crop
    d = data_dir() / "hand_crops"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"sub_{sub_id}_{r.uid}_{key}.png"

def aligned_page(template: TestTemplate, sub: Submission, page: int):
    """The scan of template page `page` aligned to the template (cached on disk; aligned once)."""
    cached = sub.aligned_pages[page] if page < len(sub.aligned_pages) else ""
    img = read_image(cached) if cached and Path(cached).exists() else None
    if img is not None:
        return img
    scan = read_image(sub.pages[page]) if page < len(sub.pages) and sub.pages[page] else None
    tmpl = read_image(template.pages[page]) if page < template.page_count else None
    if scan is None or tmpl is None:
        return None
    img, _ = align_with_score(tmpl, scan)
    img = scan if img is None else img
    out = data_dir() / "aligned"
    out.mkdir(parents=True, exist_ok=True)
    path = str(out / f"sub_{sub.id}_p{page + 1}.png")
    save_image(path, img)
    set_aligned_page(sub.id, page, path, template.page_count)
    sub.aligned_pages = (sub.aligned_pages + [""] * template.page_count)[:max(template.page_count, page + 1)]
    sub.aligned_pages[page] = path
    return img

def answer_crop(template: TestTemplate, sub: Submission, i: int) -> str:
    """Path of the crop of question i's answer box on test `sub` ('' if the page is missing)."""
    path = _crop_file(sub.id, template, i)
    if path.exists():
        return str(path)
    r = template.regions[i]
    page = r.page if 0 <= r.page < template.page_count else 0
    img = aligned_page(template, sub, page)
    if img is None:
        return ""
    save_image(str(path), crop_region(img, r.rect, pad=CROP_PADDING))
    return str(path)

def prepare_answers(template: TestTemplate, i: int, detect_blanks: bool = False,
                    progress: Callable[[int, int, str], None] = None,
                    should_stop: Callable[[], bool] = None) -> List[Answer]:
    """Crop question i on every eligible test (aligning pages that weren't aligned yet) and,
    with detect_blanks, run the blank CNN on the crops. Stops early (returns what is done) when
    should_stop() is true."""
    subs = eligible_submissions(template)
    out = []
    for n, sub in enumerate(subs):
        if should_stop and should_stop():
            break
        if progress:
            progress(n, len(subs), f"Cutting out the answers… {n + 1} of {len(subs)}")
        out.append(Answer(sub, answer_crop(template, sub, i)))
    if detect_blanks and out:
        from .blank_cnn import get_detector
        if progress:
            progress(len(out), len(out), "Looking for blank answers…")
        have = [a for a in out if a.crop]
        for a, p in zip(have, get_detector().predict([a.crop for a in have])):
            a.p_not_blank = float(p)
    return out

def predicted_blank(answers: List[Answer], threshold: float = None) -> List[int]:
    """Submission ids whose answer the CNN thinks is blank."""
    from .blank_cnn import THRESHOLD
    th = THRESHOLD if threshold is None else threshold
    return [a.sub.id for a in answers if a.p_not_blank is not None and a.p_not_blank < th]

def save_hand_grade(template: TestTemplate, i: int, sub_id: int, item_ids: List[str], comment: str = "",
                    group_id: Optional[int] = None, crop: str = "") -> Grade:
    """Grade question i of one test by hand from the checked rubric items. Replaces the
    question's AI grade if it has one (the human grade is final; the LLM never touches it)."""
    r = template.regions[i]
    items = [it.id for it in r.rubric.checked(item_ids)]
    g = get_grade(sub_id, i) or Grade(submission_id=sub_id, region_id=i)
    g.problem, g.group, g.max_points = r.problem, r.group, r.points
    g.score = r.rubric.score(items, r.points)
    g.feedback = r.rubric.feedback(items, comment)
    g.needs_human_review, g.review_reason, g.confidence = False, "", 1.0
    g.human_graded, g.source = True, "hand"
    g.rubric_items, g.comment, g.hand_group_id = items, comment.strip(), group_id
    if crop:
        g.extracted_image_path = crop
    save_grade(g)
    for dup in list_grades(sub_id):        # one grade per question (a race could have added one)
        if dup.region_id == i and dup.id != g.id:
            delete_grade(dup.id)
    refresh_status(sub_id, template)
    return g

def grade_group(template: TestTemplate, i: int, group: dict, item_ids: List[str], comment: str = "",
                crops: dict = None) -> List[Grade]:
    """Grade every member of an answer group the same way; the group remembers the choice (and a
    suggested group counts as checked: the grader saw its answers)."""
    update_hand_group(group["id"], items=list(item_ids), comment=comment, checked=True)
    crops = crops or {}
    return [save_hand_grade(template, i, sid, item_ids, comment, group["id"], crops.get(sid, ""))
            for sid in group["members"]]

def hand_graded(template: TestTemplate, i: int) -> dict:
    """{submission id: Grade} of the tests whose question i was graded by a human."""
    return {sid: g for sid, g in region_grades(template.id, i).items() if g.human_graded}

def groups(template: TestTemplate, i: int) -> List[dict]:
    return list_hand_groups(template.id, template.regions[i].uid)

def ensure_blank_group(template: TestTemplate, i: int) -> int:
    """Id of question i's Blank group, made (empty) if missing: every question has one, so blank
    answers the CNN missed can be added to it."""
    g = next((g for g in groups(template, i) if g["is_blank"]), None)
    return g["id"] if g else add_hand_group(template.id, template.regions[i].uid, BLANK_LABEL, [], is_blank=True)

def hand_grade_count(template: TestTemplate, i: int, sub_ids=None) -> int:
    """How many Hand Grade grades question i has (only those of sub_ids if given)."""
    return sum(1 for sid, g in hand_graded(template, i).items()
               if g.source == "hand" and (sub_ids is None or sid in sub_ids))

def ungrade(template: TestTemplate, i: int, sub_ids=None) -> int:
    """Delete the Hand Grade grades of question i (only those of sub_ids if given); grades set in
    Review and AI grades stay. Returns how many were deleted."""
    n = 0
    for sid, g in hand_graded(template, i).items():
        if g.source == "hand" and (sub_ids is None or sid in sub_ids):
            delete_grade(g.id)
            refresh_status(sid, template)
            n += 1
    return n

def dissolve_group(template: TestTemplate, i: int, group: dict) -> int:
    """Its answers become ungrouped and lose their hand grades (they are graded again); the Blank
    group stays, empty and with no remembered grade. Returns the number of grades deleted."""
    n = ungrade(template, i, set(group["members"]))
    if group["is_blank"]:
        set_hand_group_members(group["id"], [])
        update_hand_group(group["id"], items=[], comment="")
    else:
        delete_hand_group(group["id"])
    return n

def start_over(template: TestTemplate, i: int) -> int:
    """Grade question i from scratch: delete its groups and its hand grades (grades set in
    Review and AI grades stay) and go back to the start (its category is kept). Returns the
    number of hand grades deleted. Can't be undone."""
    for g in groups(template, i):
        delete_hand_group(g["id"])
    n = ungrade(template, i)
    set_hand_stage(template.id, template.regions[i].uid, "start")
    return n

def progress_text(template: TestTemplate, i: int, total: int) -> str:
    n = len(hand_graded(template, i))
    return f"{n} / {total} graded by hand"
