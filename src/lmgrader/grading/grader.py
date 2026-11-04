"""Grading orchestration (multi-page, resumable)."""
import json
import re
from typing import Dict, List, Optional
import cv2
import numpy as np
from ..models import TestTemplate, Submission, Grade
from ..template.alignment import read_image, align_with_score, crop_region, save_image
from ..template.template import question_context, siblings
from ..llm.client import LLMClient, image_to_b64, prepare_crop, downscale_for_llm
from ..llm.back_pages import route_back_page, grade_with_continuation, bbox_to_pixels
from ..db import save_grade, list_grades, list_submissions, update_submission
from .back_page import analyze_back_page
from ..config import data_dir, MIN_ALIGN_INLIERS, CROP_PADDING, CONTINUATION_PATTERNS, LLM_CONFIG
from ..db import get_exam_instructions, get_exam_strictness

def _add_reason(g: Grade, reason: str):
    g.needs_human_review = True
    if reason not in g.review_reason:
        g.review_reason = "; ".join(r for r in (g.review_reason, reason) if r)

_CONT_RE = [re.compile(p, re.IGNORECASE) for p in CONTINUATION_PATTERNS]

def mentions_continuation(*texts: str) -> bool:
    """Deterministic check for 'see back' / 'continued on next page' / 'ver atrás' etc."""
    return any(rx.search(t or "") for t in texts for rx in _CONT_RE)

def is_full_marks(g: Grade) -> bool:
    return g.max_points > 0 and g.score >= g.max_points - 1e-9

def apply_review_rules(g: Grade, res: dict, threshold: float, full_marks_floor: Optional[float] = None):
    """Route the grade to a human when the model is unsure or the work continues elsewhere.
    These rules don't depend on the model choosing to set needs_human_review.
    full_marks_floor: when set, a full-marks grade is not sent to a human for low confidence
    unless its confidence is also below this floor (the other rules still apply)."""
    if res.get("continues_elsewhere") or mentions_continuation(g.transcription):
        _add_reason(g, "work_continues_elsewhere")
    if threshold > 0 and g.confidence < threshold:
        if not (is_full_marks(g) and full_marks_floor is not None and g.confidence >= full_marks_floor):
            _add_reason(g, f"low_confidence ({g.confidence:.2f})")

def review_settings():
    """(confidence threshold, full-marks floor or None) from the settings."""
    threshold = float(LLM_CONFIG.get("review_confidence", 0.7) or 0)
    floor = (float(LLM_CONFIG.get("full_marks_min_confidence", 0.3) or 0)
             if LLM_CONFIG.get("full_marks_skip_review") else None)
    return threshold, floor

def reapply_full_marks_rules() -> int:
    """Apply the full-marks settings to grades already saved: an AI grade with full marks that
    was sent to a human only for low confidence (above the floor) or for writing on the back
    page is no longer flagged. Human-graded grades and other reasons are left alone.
    Returns the number of grades un-flagged."""
    _, floor = review_settings()
    ignore_back = bool(LLM_CONFIG.get("full_marks_ignore_back", True))
    if floor is None and not ignore_back:
        return 0
    cleared = 0
    for sub in list_submissions():
        back_dropped = False
        grades = list_grades(sub.id)
        for g in grades:
            if g.human_graded or not g.needs_human_review or not is_full_marks(g):
                continue
            parts = [p for p in g.review_reason.split("; ") if p]
            keep = []
            for p in parts:
                if floor is not None and p.startswith("low_confidence") and g.confidence >= floor:
                    continue
                if ignore_back and p.startswith("work_on_back"):
                    back_dropped = True
                    continue
                keep.append(p)
            if len(keep) == len(parts):
                continue
            g.review_reason = "; ".join(keep)
            g.needs_human_review = bool(keep)
            save_grade(g)
            if not keep:
                cleared += 1
        # same as after grading: the test-level "check the back page" flag goes too when no
        # question is still waiting on the back page
        if (back_dropped and sub.has_work_on_back and not sub.back_reviewed and
                not any("work_on_back" in g.review_reason for g in grades)):
            sub.back_reviewed = True
            update_submission(sub)
    return cleared

def align_pages(template: TestTemplate, submission: Submission):
    """Align every scanned page to its template page. Returns (aligned images, inliers per page)
    and stores the aligned page paths on the submission."""
    out_dir = data_dir() / "aligned"
    out_dir.mkdir(parents=True, exist_ok=True)
    aligned, inliers, paths = [], [], []
    scans = submission.pages
    for p, t_path in enumerate(template.pages):
        t_img = read_image(t_path)
        s_img = read_image(scans[p]) if p < len(scans) else None
        if t_img is None:
            raise ValueError(f"Missing template page {p + 1}: {t_path}")
        if s_img is None:
            aligned.append(None); inliers.append(0); paths.append("")
            continue
        a, n = align_with_score(t_img, s_img)
        if a is None:
            a = s_img  # fall back to raw scan; grades on this page get flagged
        path = str(out_dir / f"sub_{submission.id}_p{p + 1}.png")
        save_image(path, a)
        aligned.append(a); inliers.append(n); paths.append(path)
    submission.aligned_pages = paths
    submission.aligned_front_path = paths[0] if paths else ""
    return aligned, inliers

class BackWork:
    """What the back pages of a submission contribute to each region.
    crops[i]: back-page images continuing region i; flags[i]: review reasons for region i."""
    def __init__(self):
        self.crops: Dict[int, list] = {}
        self.flags: Dict[int, List[str]] = {}
        self.levels: List[str] = []

    def flag(self, i: int, reason: str):
        self.flags.setdefault(i, [])
        if reason not in self.flags[i]:
            self.flags[i].append(reason)

def _route_cached(llm, sub_id, b, img, labels):
    """route_back_page, cached on disk so a resumed grading doesn't ask the LLM again."""
    path = data_dir() / "back_routes" / f"sub_{sub_id}_b{b}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    route = route_back_page(llm, image_to_b64(downscale_for_llm(img)[0]), labels)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(route), encoding="utf-8")
    return route

def _stack(images):
    if len(images) == 1:
        return images[0]
    w = max(im.shape[1] for im in images)
    padded = [cv2.copyMakeBorder(im, 0, 10, 0, w - im.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))
              for im in images]
    return np.vstack(padded)

def analyze_backs(llm, template: TestTemplate, submission: Submission, todo: set) -> BackWork:
    """Classify every back page; for pages with real work ask the LLM which problems it continues.
    backs[i] is the back of template page i (extra blank pages count as the last page).
    Only routes when some region in `todo` (still ungraded) could be affected."""
    bw = BackWork()
    labels = [r.problem for r in template.regions]
    last = max(0, template.page_count - 1)
    for b, path in enumerate(submission.backs):
        if not path:
            bw.levels.append("")
            continue
        info = analyze_back_page(path)
        bw.levels.append(info["level"])
        if info["level"] != "work":
            continue
        page = min(b, last)
        on_page = [i for i, r in enumerate(template.regions) if min(r.page, last) == page]
        if not todo:
            continue
        img = read_image(path)
        try:
            route = _route_cached(llm, submission.id, b, img, labels)
        except Exception:
            route = None   # routing unavailable: a human checks every question on that page
        if route is None:
            for i in on_page:
                bw.flag(i, "work_on_back")
            continue
        h, w = img.shape[:2]
        for it in route.get("items", []):
            if it.get("problem") is None:
                continue
            for i, r in enumerate(template.regions):
                if r.problem == it["problem"]:
                    x, y, cw, ch = bbox_to_pixels(it.get("bbox"), w, h)
                    bw.crops.setdefault(i, []).append(img[y:y + ch, x:x + cw])
                    bw.flag(i, "work_on_back")
        if route.get("unlabeled") or not route.get("items"):
            for i in on_page:
                bw.flag(i, "work_on_back (unlabeled)")
    return bw

def grading_order(template: TestTemplate) -> List[int]:
    """Region indices in grading order: the parts of a question together and in part order
    (a, b, c...), so every part is graded after the earlier parts whose notes it receives."""
    order, seen = [], set()
    for i in range(len(template.regions)):
        for j in siblings(template, i):
            if j not in seen:
                seen.add(j); order.append(j)
    return order

def prior_part_notes(template: TestTemplate, i: int, graded: Dict[int, Grade]) -> str:
    """Notes left by the graders of the parts before region i (same main question)."""
    sib = siblings(template, i)
    out = []
    for j in sib[:sib.index(i)]:
        g = graded.get(j)
        if g is not None and g.next_part_notes.strip():
            out.append(f"Part {template.regions[j].problem}: {g.next_part_notes.strip()}")
    return "\n".join(out)

class Grader:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def grade_submission(self, template: TestTemplate, submission: Submission,
                         progress_cb=None, should_stop=None) -> List[Grade]:
        """Grade every region. Regions that already have a saved grade are skipped, so an
        interrupted run (crash, power loss, cancel) resumes where it stopped."""
        aligned, inliers = align_pages(template, submission)
        done = {g.region_id: g for g in list_grades(submission.id)}

        # back pages: blank / minor (a name, a doodle) / work. Work is routed to its problems and
        # graded together with the front; those problems always go to a human for confirmation.
        todo = {i for i in range(len(template.regions)) if i not in done}
        back = analyze_backs(self.llm, template, submission, todo)
        submission.has_work_on_back = submission.has_work_on_back or "work" in back.levels
        ctx = self._context(template)
        back_skipped = False
        graded: Dict[int, Grade] = dict(done)
        total = len(template.regions)
        for n, i in enumerate(grading_order(template), start=1):
            if i in done:
                continue
            if should_stop and should_stop():
                break
            g, skipped = self._grade_region(template, submission, i, aligned, inliers, back, graded, ctx)
            back_skipped = back_skipped or skipped
            save_grade(g)   # persisted per region: this is the resume point
            graded[i] = g
            if progress_cb:
                progress_cb(n, total)
        # every question the back-page work concerns got full marks: the test-level "check the
        # back page" flag is resolved too (the caller saves the submission)
        if (back_skipped and len(graded) == total and
                not any("work_on_back" in g.review_reason for g in graded.values())):
            submission.back_reviewed = True
        return [graded[i] for i in sorted(graded)]

    def regrade_region(self, template: TestTemplate, submission: Submission, i: int) -> Grade:
        """Ask the LLM again for region i (Review's "Retry AI grade"), with the same inputs as the
        first run. The new grade replaces the saved one (a human grade included) only when the LLM
        answers: on an LLM error the old grade is kept and RuntimeError is raised."""
        aligned, inliers = align_pages(template, submission)
        old = {g.region_id: g for g in list_grades(submission.id)}
        back = analyze_backs(self.llm, template, submission, {i})
        g, _ = self._grade_region(template, submission, i, aligned, inliers, back, old, self._context(template))
        if "llm_error" in g.review_reason:
            raise RuntimeError(g.feedback or "LLM error")
        if i in old:
            g.id = old[i].id
        save_grade(g)
        return g

    @staticmethod
    def _context(template: TestTemplate) -> dict:
        threshold, floor = review_settings()
        ext_dir = data_dir() / "extracted"
        ext_dir.mkdir(parents=True, exist_ok=True)
        return dict(instructions=get_exam_instructions(template.exam_name),
                    strictness=get_exam_strictness(template.exam_name),
                    threshold=threshold, full_marks_floor=floor,
                    ignore_back_full=bool(LLM_CONFIG.get("full_marks_ignore_back", True)), ext_dir=ext_dir)

    def _grade_region(self, template, submission, i, aligned, inliers, back, graded, ctx):
        """Grade region i (not saved). Returns (grade, True if its back-page flag was skipped
        because of full marks)."""
        region = template.regions[i]
        sib = siblings(template, i)
        has_next = sib.index(i) < len(sib) - 1
        prior = prior_part_notes(template, i, graded)
        page = region.page if 0 <= region.page < len(aligned) else 0
        img = aligned[page] if aligned else None
        if img is None:
            g = Grade(submission_id=submission.id, region_id=i, problem=region.problem, group=region.group,
                      score=0.0, max_points=region.points, feedback="Page missing from scan.",
                      needs_human_review=True, review_reason="missing_page")
        else:
            question = question_context(template, i)   # parts: main question + earlier parts
            crop = crop_region(img, region.rect, pad=CROP_PADDING)
            ext_path = str(ctx["ext_dir"] / f"sub_{submission.id}_r{i}.png")
            save_image(ext_path, crop)
            kw = dict(question=question, instructions=ctx["instructions"], notes=region.grading_notes,
                      prior_notes=prior, has_next=has_next, strictness=ctx["strictness"])
            if i in back.crops:
                back_img = _stack(back.crops[i])
                save_image(str(ctx["ext_dir"] / f"sub_{submission.id}_r{i}_back.png"), back_img)
                res = grade_with_continuation(
                    self.llm, image_to_b64(prepare_crop(crop)), image_to_b64(prepare_crop(back_img)),
                    region.problem, region.group, region.rubric, region.points, **kw)
            else:
                res = self.llm.grade_answer(image_to_b64(prepare_crop(crop)), region.problem, region.group,
                                            region.rubric, region.points, **kw)
            g = Grade(
                submission_id=submission.id, region_id=i, problem=region.problem, group=region.group,
                score=res["score"], max_points=region.points, feedback=res["feedback"],
                needs_human_review=bool(res["needs_human_review"]),
                review_reason=res["review_reason"] if res["needs_human_review"] else "",
                extracted_image_path=ext_path, transcription=res.get("transcription", ""),
                confidence=float(res.get("confidence", 1.0)),
                next_part_notes=res.get("next_part_notes", "") if has_next else "",
            )
            apply_review_rules(g, res, ctx["threshold"], ctx["full_marks_floor"])
            if inliers[page] < MIN_ALIGN_INLIERS:
                _add_reason(g, f"poor_alignment ({inliers[page]} inliers)")
        skipped = False
        if not submission.back_reviewed and back.flags.get(i):
            if ctx["ignore_back_full"] and is_full_marks(g):
                skipped = True      # nothing to gain from the back: no human needed
            else:
                for reason in back.flags[i]:
                    _add_reason(g, reason)
        return g, skipped
