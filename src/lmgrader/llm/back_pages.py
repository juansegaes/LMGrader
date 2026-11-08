"""Work written on the back of a page.

* route_back_page: which exam problems does the work on this back page belong to?
* grade_with_continuation: grade an answer box together with its continuation from the back.

Both call LLMClient._chat directly so client.py stays the single place for the HTTP logic
(and LLMClient.abort() — the Stop button — also interrupts these calls).
"""
import re
from typing import List, Optional
from .client import (GRADE_SCHEMA, GRADE_SCHEMA_NEXT, GRADER_SYSTEM, PARTS_NOTE, NEXT_PART_STEP, CROSSED_OUT_NOTE,
                     FEEDBACK_LIMIT, run_grading, grader_system, scoring_step,
                     _instructions_block, _prior_notes_block, _image_part, _extract_json, _strip_reasoning)

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "box": {"type": "array", "items": {"type": "number"}},
                    "summary": {"type": "string"},
                },
                "required": ["problem", "box", "summary"],
            },
        },
        "unlabeled": {"type": "boolean"},
    },
    "required": ["items", "unlabeled"],
}

def _norm(label: str) -> str:
    """'Q2(a)' / '2a)' / '2.a' -> '2a'."""
    s = re.sub(r"[^0-9a-z]", "", (label or "").lower())
    return s[1:] if s.startswith("q") and len(s) > 1 and s[1].isdigit() else s

def resolve_labels(problem: str, labels: List[str]) -> List[str]:
    """Map what the model/student wrote to template labels. A bare number ('1') means every
    part of that problem ('1a', '1b'); unknown labels map to nothing."""
    p = _norm(problem)
    if not p:
        return []
    exact = [l for l in labels if _norm(l) == p]
    if exact:
        return exact
    if p.isdigit():
        return [l for l in labels if _norm(l).startswith(p) and _norm(l)[len(p):len(p) + 1].isalpha()]
    return []

def route_back_page(llm, image_b64: str, labels: List[str]) -> dict:
    """Returns {"items": [{"problem": label|None, "bbox": [x1,y1,x2,y2] (0-1000)|None, "summary"}],
    "unlabeled": bool}. Items whose label could not be matched have problem None."""
    listing = ", ".join(labels)
    prompt = (
        "This image is the BACK side of an exam page. Students use it to continue answers that did "
        "not fit in the boxes on the front, and they are asked to label each piece of work with its "
        "problem and part (e.g. '2a)', '3b.', '1)').\n"
        f"The exam's problems are: {listing}.\n"
        "List every separate piece of work on this page. For each give:\n"
        "- problem: the problem it belongs to, written exactly as one of the labels above. Use the "
        "student's label if there is one; a bare number like '1)' may stand for a problem with "
        "parts — then use the bare number. If unlabeled, infer it from the content only if you are "
        "sure, otherwise use an empty string.\n"
        "- box: [x1, y1, x2, y2] around that piece of work, coordinates 0-1000 relative to the image.\n"
        "- summary: a few words on what the work is.\n"
        "Ignore the student's name, stray marks and anything crossed out. Set unlabeled true if any "
        "piece of work has no problem label.\n"
        "Respond ONLY as JSON: {\"items\": [{\"problem\": string, \"box\": [x1,y1,x2,y2], "
        "\"summary\": string}], \"unlabeled\": boolean}"
    )
    text = llm._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                   _image_part(image_b64)]}], ROUTE_SCHEMA, "back_page")
    d = _extract_json(text)
    if d is None:
        raise ValueError(f"Could not parse back-page routing:\n{_strip_reasoning(text)[:300]}")
    items, unlabeled = [], bool(d.get("unlabeled", False))
    for it in d.get("items", []) or []:
        box = it.get("box") or []
        try:
            box = [max(0.0, min(1000.0, float(v))) for v in box[:4]]
        except (TypeError, ValueError):
            box = []
        bbox = box if len(box) == 4 and box[2] > box[0] and box[3] > box[1] else None
        targets = resolve_labels(str(it.get("problem", "")), labels)
        if not targets:
            unlabeled = True
            items.append({"problem": None, "bbox": bbox, "summary": str(it.get("summary", ""))})
        for t in targets:
            items.append({"problem": t, "bbox": bbox, "summary": str(it.get("summary", ""))})
    return {"items": items, "unlabeled": unlabeled}

def grade_with_continuation(llm, front_b64: str, back_b64: str, problem: str, group: str, rubric: str,
                            max_points: float, question: str = "", instructions: str = "",
                            notes: str = "", prior_notes: str = "", has_next: bool = False,
                            strictness: int = 10) -> dict:
    """Like LLMClient.grade_answer, but the second image is the continuation of the answer that
    the student wrote on the back of the page."""
    rubric = rubric.as_text(max_points) if hasattr(rubric, "as_text") else rubric
    prompt = (
        f"Problem {problem} (section '{group}'). Maximum points: {max_points:g}.\n\n"
        f"Question:\n{question or '(question text not provided; infer it from the image)'}\n\n"
        f"Rubric:\n{rubric or 'Grade based on correctness and completeness.'}\n\n"
        + _instructions_block(instructions, notes) + PARTS_NOTE + _prior_notes_block(prior_notes) +
        "IMAGE 1 is the student's answer box on the front of the page. IMAGE 2 is work the student "
        f"wrote on the BACK of the page for this problem ({problem}); treat it as the continuation "
        "of the same answer and grade both together. Ignore anything in image 2 that clearly "
        "belongs to another problem.\n\n"
        + CROSSED_OUT_NOTE +
        "Steps:\n"
        "1. Transcribe what the student wrote in both images (LaTeX for math) into 'transcription', "
        "marking the back part with '[back]' and leaving out crossed-out work.\n"
        + scoring_step(strictness, "2. Apply each rubric criterion (and the professor's instructions) "
                                   "and sum the points into 'score'.\n") +
        "3. Give short, specific 'feedback' naming which criteria were met or missed " + FEEDBACK_LIMIT + ".\n"
        "4. 'confidence': 0 to 1, how sure you are that the score is right.\n"
        "5. 'continues_elsewhere': true only if the work continues somewhere beyond these two images.\n"
        + (NEXT_PART_STEP if has_next else "") +
        "Set needs_human_review true (and explain in review_reason) if anything is illegible or ambiguous.\n"
        "Respond ONLY as JSON with keys: transcription, score, feedback, needs_human_review, "
        "review_reason, confidence, continues_elsewhere" + (", next_part_notes." if has_next else ".")
    )
    try:
        return run_grading(llm, [{"role": "system", "content": grader_system(strictness)},
                                 {"role": "user", "content": [{"type": "text", "text": prompt},
                                                              _image_part(front_b64), _image_part(back_b64)]}],
                           GRADE_SCHEMA_NEXT if has_next else GRADE_SCHEMA, max_points)
    except Exception as e:
        return {"score": 0.0, "feedback": f"LLM error: {e}", "needs_human_review": True,
                "review_reason": "llm_error", "transcription": "", "confidence": 0.0,
                "continues_elsewhere": False, "next_part_notes": ""}

def bbox_to_pixels(bbox: Optional[list], width: int, height: int, pad: float = 0.03):
    """0-1000 box -> (x, y, w, h) pixels with padding; None -> whole page."""
    if not bbox:
        return (0, 0, width, height)
    x1, y1, x2, y2 = bbox
    px, py = pad * width, pad * height
    x1 = max(0, int(x1 / 1000 * width - px)); y1 = max(0, int(y1 / 1000 * height - py))
    x2 = min(width, int(x2 / 1000 * width + px)); y2 = min(height, int(y2 / 1000 * height + py))
    return (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
