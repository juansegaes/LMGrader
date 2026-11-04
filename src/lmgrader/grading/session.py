"""Grading session: split uploads into tests, identify students, grade — all persisted.

Every step writes to the database immediately, so closing the app (or a crash / power loss)
never loses work: on restart the queue is rebuilt from the `submissions` table and grading
resumes at the first region without a saved grade.
"""
from typing import List, Optional, Sequence, Tuple, Union
from ..models import (Submission, TestTemplate, Student, BLANK, double_sided_layout, SUB_NEW, SUB_IDENTIFIED,
                      SUB_READY, SUB_GRADING, SUB_GRADED, SUB_ERROR)
from ..db import (add_submission, update_submission, list_submissions_by_status, load_template, connect,
                  delete_grades)
from .identify import identify
from .grader import Grader

def split_by_layout(pages: Sequence, layout: Sequence[int]) -> Tuple[List[Tuple[list, list]], list]:
    """Cut a flat list of scanned pages into tests following a scan layout (template page index
    or BLANK per scanned page, e.g. [0, BLANK, 1, BLANK] for a double-sided 2-page test).
    Returns ([(front pages, back pages)], leftover pages that don't make a full test).
    fronts[i] is the scan of template page i; backs[i] is the BLANK page right after template
    page i ('' if none); blank pages not directly after a template page are appended to backs.
    backs is [] when the test has no blank pages at all."""
    layout = list(layout) or [0]
    n = len(layout)
    page_count = sum(1 for p in layout if p != BLANK)
    tests = []
    full = len(pages) - len(pages) % n
    for i in range(0, full, n):
        chunk = pages[i:i + n]
        fronts, backs, extras, prev = [None] * page_count, [""] * page_count, [], None
        for k, entry in enumerate(layout):
            if entry == BLANK:
                if prev is not None and backs[prev] == "":
                    backs[prev] = chunk[k]
                else:
                    extras.append(chunk[k])
                prev = None
            else:
                fronts[entry] = chunk[k]
                prev = entry
        has_blank = any(p == BLANK for p in layout)
        tests.append((fronts, backs + extras if has_blank else []))
    return tests, list(pages[full:])

def split_pages(pages: Sequence, pages_per_test: int, duplex: bool = False
                ) -> Tuple[List[Tuple[list, list]], list]:
    """Old interface: every page in order, or (duplex) every page followed by its back side."""
    n = max(1, pages_per_test)
    return split_by_layout(pages, double_sided_layout(n) if duplex else list(range(n)))

def layout_of(spec: Union[TestTemplate, Sequence[int], int], duplex: bool = False) -> List[int]:
    if isinstance(spec, TestTemplate):
        return spec.layout
    if isinstance(spec, int):
        return double_sided_layout(spec) if duplex else list(range(max(1, spec)))
    return list(spec)

def create_submissions(exam_name: str, pages: List[str], spec: Union[TestTemplate, Sequence[int], int],
                       duplex: bool = False, source: str = "") -> Tuple[List[Submission], List[str]]:
    """Split pages into tests (spec = template, scan layout, or pages per test) and persist one
    'new' submission per test."""
    tests, leftover = split_by_layout(pages, layout_of(spec, duplex))
    subs = []
    for fronts, backs in tests:
        sub = Submission(exam_name=exam_name, status=SUB_NEW, scan_pages=fronts, back_pages=backs, source=source)
        add_submission(sub)
        subs.append(sub)
    return subs, leftover

def identify_submission(sub: Submission, versions: List[TestTemplate], students: List[Student], llm=None,
                        save_crops: bool = True) -> Submission:
    """Detect version + read the student ID / name. A confident match is ready to grade;
    anything else waits in 'identified' until a human confirms the student."""
    forced = None
    if sub.version_confirmed:        # never override a version a human picked
        forced = next((v for v in versions if v.id == sub.template_id), None) or load_template(sub.template_id)
    ident = identify(sub.pages, versions, students, llm, save_crops_for=sub.id if save_crops and sub.id else None,
                     forced=forced)
    if not sub.version_confirmed:
        sub.version_read = ident.version_read
    if ident.template is None:
        sub.status, sub.error = SUB_ERROR, ident.error or "Could not detect the exam version"
        update_submission(sub)
        return sub
    sub.template_id = ident.template.id
    sub.error = ident.error
    if ident.name is not None:
        sub.name_read, sub.name_match_score = ident.name.name_read, ident.name.score
        sub.id_read = ident.name.id_read
    if not sub.name_confirmed:       # never override a student a human picked
        if ident.name is not None:
            sub.student_id = ident.name.student.id if ident.name.student else None
            sub.name_confirmed = sub.student_id is not None and not ident.name.needs_confirm
        else:
            sub.name_confirmed = False
    if not ident.alignment_ok:
        sub.error = (sub.error + "; " if sub.error else "") + f"weak alignment ({ident.inliers} matches)"
    if ident.version_uncertain:
        sub.error = (sub.error + "; " if sub.error else "") + "version field unreadable — check the version"
    ok = sub.name_confirmed and sub.student_id and not ident.version_uncertain
    sub.status = SUB_READY if ok else SUB_IDENTIFIED
    update_submission(sub)
    return sub

def set_version(sub: Submission, template: TestTemplate) -> Submission:
    """Human picks the exam version. Grades made against another version no longer apply and are
    deleted (the test is graded again); name/ID crops are re-cut from the new version's fields."""
    changed = sub.template_id != template.id
    sub.template_id, sub.version_confirmed, sub.version_read = template.id, True, "manual"
    sub.error = "; ".join(p for p in (sub.error or "").split("; ") if p and not p.startswith("version field"))
    if changed:
        delete_grades(sub.id)
        sub.aligned_pages, sub.aligned_front_path = [], ""
        sub.error = ""
        try:
            identify(sub.pages, [template], [], None, save_crops_for=sub.id, forced=template)   # crops only
        except Exception:
            pass
    # a changed version is graded again; an unchanged one only clears the "check the version" hold
    if sub.status != SUB_GRADING and (changed or sub.status in (SUB_NEW, SUB_IDENTIFIED, SUB_ERROR)):
        sub.status = SUB_READY if sub.name_confirmed and sub.student_id else SUB_IDENTIFIED
    update_submission(sub)
    return sub

def version_hold(sub: Submission) -> bool:
    """The version field couldn't be read and nobody has picked the version yet."""
    return not sub.version_confirmed and "version field unreadable" in (sub.error or "")

def confirm_student(sub: Submission, student_id: Optional[int]):
    """Human sets/confirms the student."""
    sub.student_id = student_id
    sub.name_confirmed = student_id is not None
    if sub.status in (SUB_NEW, SUB_IDENTIFIED, SUB_READY) and sub.template_id:
        sub.status = SUB_READY if student_id and not version_hold(sub) else SUB_IDENTIFIED
    update_submission(sub)

def reset_for_regrade(sub: Submission, keep_human: bool = True) -> int:
    """Prepare a graded (or partly graded / failed) test to be graded again from scratch, e.g.
    after the answer key, notes or strictness changed. The AI grades are deleted; with keep_human,
    questions a human graded are kept (grading skips them). The "back page reviewed" mark is
    cleared (it may hide back-page work from the new grades; grading sets it again when every
    back-flagged question gets full marks). The test goes back to the grading queue.
    Returns the number of grades deleted."""
    if sub.status in (SUB_NEW, SUB_GRADING) or not sub.template_id:
        return 0
    c = connect()
    q = "DELETE FROM grades WHERE submission_id=?" + (" AND COALESCE(human_graded,0)=0" if keep_human else "")
    n = c.execute(q, (sub.id,)).rowcount
    c.commit(); c.close()
    sub.back_reviewed = False
    sub.status = SUB_READY if sub.name_confirmed and sub.student_id else SUB_IDENTIFIED
    sub.error = ""
    update_submission(sub)
    return n

def _drop_aborted_grades(sub_id: int, before_ids: set):
    """A Stop aborts the in-flight LLM request; the grader records that as an 'llm_error' grade.
    Remove such grades created in this run so the question is simply graded again on resume."""
    c = connect()
    rows = c.execute("SELECT id, review_reason FROM grades WHERE submission_id=?", (sub_id,)).fetchall()
    for r in rows:
        if r["id"] not in before_ids and "llm_error" in (r["review_reason"] or ""):
            c.execute("DELETE FROM grades WHERE id=?", (r["id"],))
    c.commit(); c.close()

def grade_one(grader: Grader, sub: Submission, progress_cb=None, should_stop=None) -> Submission:
    template = load_template(sub.template_id)
    if template is None:
        sub.status, sub.error = SUB_ERROR, "Template missing"
        update_submission(sub)
        return sub
    c = connect()
    before = {r["id"] for r in c.execute("SELECT id FROM grades WHERE submission_id=?", (sub.id,))}
    c.close()
    sub.status = SUB_GRADING
    update_submission(sub)
    stopped = lambda: bool(should_stop and should_stop())
    try:
        grades = grader.grade_submission(template, sub, progress_cb, should_stop)
        complete = len(grades) == len(template.regions)
        sub.status = SUB_GRADED if complete and not stopped() else SUB_READY   # stopped midway: resume later
        sub.error = ""
    except Exception as e:
        if stopped():
            sub.status, sub.error = SUB_READY, ""
        else:
            sub.status, sub.error = SUB_ERROR, str(e)
    if stopped():
        _drop_aborted_grades(sub.id, before)
        if sub.status == SUB_GRADED:
            sub.status = SUB_READY
    update_submission(sub)
    return sub

def recover_interrupted() -> int:
    """On startup: submissions left in 'grading' by a crash go back to 'ready' (grades already
    saved are kept and skipped when grading resumes)."""
    stuck = list_submissions_by_status(statuses=[SUB_GRADING])
    for s in stuck:
        s.status = SUB_READY
        update_submission(s)
    return len(stuck)
