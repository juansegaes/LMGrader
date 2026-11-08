"""Answer groups suggested without an LLM.

The first time a question is opened in Hand Grade the human picks its category:
  blank   the blank answers are found (grading.blank_cnn); the human groups the rest;
  short   blank answers are found, every other answer is read on this computer (TrOCR, only
          what the student wrote: the printed template is removed first) and answers that read
          the same are grouped (2 or more answers per group);
  choice  multiple choice: the choice each test marked is found on the scan (grading.marks)
          and tests are grouped by it ("B", "A + C" when more than one is marked, Blank).
Suggested groups are normal hand groups with an auto_key and checked=0: the human checks each
one (takes out answers that don't belong, presses Looks right) before or while grading.
Answers that are already in a group or graded by hand are never moved; unclear answers stay
ungrouped and are graded one by one.
"""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..config import CROP_PADDING
from ..db import add_hand_group, set_hand_group_members, update_hand_group
from ..models import TestTemplate
from ..template.alignment import read_image
from . import marks
from .hand import Answer, BLANK_LABEL, aligned_page, groups, hand_graded

KIND_BLANK, KIND_SHORT, KIND_CHOICE = "blank", "short", "choice"
KINDS = (KIND_BLANK, KIND_SHORT, KIND_CHOICE)
BLANK_KEY = "blank"
MIN_READ_CONFIDENCE = 0.5     # TrOCR readings below this are not trusted for grouping
MIN_SHORT_GROUP = 2           # a short-answer group needs 2+ answers (a multiple choice one: 1)

@dataclass
class Suggestion:
    key: str                  # what the answers have in common ("choice:B", "text:x=2", "blank")
    label: str                # group name shown to the human
    members: List[int] = field(default_factory=list)
    is_blank: bool = False

@dataclass
class Summary:
    """What a suggestion run did (shown to the human)."""
    grouped: int = 0          # answers put in a group
    groups: int = 0           # groups that got answers
    unclear: int = 0          # answers left ungrouped (unsure mark, unreadable, unique reading)
    skipped: int = 0          # already grouped or graded by hand: not touched
    no_reader: bool = False   # short answer: the handwriting reader is not available
    stopped: bool = False

def normalize_answer(text: str) -> str:
    """Readings compared for grouping: case, spaces and surrounding punctuation don't count."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = s.replace("−", "-").replace("–", "-").replace("×", "x")
    s = re.sub(r"\s+", "", s)
    return s.strip(".,;:!?'\"`")

def choice_key(labels: List[str]) -> str:
    return "choice:" + "+".join(labels)

def choice_suggestions(template: TestTemplate, i: int, answers: List[Answer], choices: List[dict],
                       progress: Callable = None, should_stop: Callable = None) -> tuple:
    """([Suggestion], answers left unclear) for a multiple choice question."""
    r = template.regions[i]
    page = r.page if 0 <= r.page < template.page_count else 0
    tmpl = read_image(template.pages[page]) if template.page_count else None
    if tmpl is None or len(choices) < 2:
        return [], len(answers)
    out: Dict[str, Suggestion] = {}
    unclear = 0
    for n, a in enumerate(answers):
        if should_stop and should_stop():
            break
        if progress:
            progress(n, len(answers), f"Finding the marked choices… {n + 1} of {len(answers)}")
        img = aligned_page(template, a.sub, page)
        picked = None if img is None else marks.decide([marks.mark_score(img, tmpl, c["rect"]) for c in choices])
        if picked is None:
            unclear += 1
            continue
        if not picked:
            key, label, blank = BLANK_KEY, BLANK_LABEL, True
        else:
            labels = [choices[k]["label"] for k in picked]
            key, label, blank = choice_key(labels), " + ".join(labels), False
        out.setdefault(key, Suggestion(key, label, is_blank=blank)).members.append(a.sub.id)
    order = {choice_key([c["label"]]): k for k, c in enumerate(choices)}
    return sorted(out.values(), key=lambda s: (not s.is_blank, order.get(s.key, len(order)), s.key)), unclear

def short_suggestions(template: TestTemplate, i: int, answers: List[Answer], reader,
                      progress: Callable = None, should_stop: Callable = None) -> tuple:
    """([Suggestion], answers left unclear) for a short answer question: answers whose readings
    match (normalize_answer) are grouped."""
    r = template.regions[i]
    page = r.page if 0 <= r.page < template.page_count else 0
    tmpl = read_image(template.pages[page]) if template.page_count else None
    readings: Dict[str, List[tuple]] = {}
    unclear = 0
    for n, a in enumerate(answers):
        if should_stop and should_stop():
            break
        if progress:
            progress(n, len(answers), f"Reading the answers… {n + 1} of {len(answers)}")
        img = aligned_page(template, a.sub, page)
        if img is None or tmpl is None:
            unclear += 1
            continue
        ink = marks.student_ink(img, tmpl, r.rect, pad=CROP_PADDING)
        text, conf = reader.read(ink) if (ink[:, :, 0] < 128).sum() >= 30 else ("", 0.0)
        key = normalize_answer(text)
        if not key or conf < MIN_READ_CONFIDENCE:
            unclear += 1
            continue
        readings.setdefault(key, []).append((a.sub.id, text.strip()))
    out = []
    for key, got in readings.items():
        if len(got) < MIN_SHORT_GROUP:
            unclear += len(got)
            continue
        shown = Counter(t for _, t in got).most_common(1)[0][0]
        out.append(Suggestion(f"text:{key}", f"“{shown}”", [sid for sid, _ in got]))
    out.sort(key=lambda s: -len(s.members))
    return out, unclear

def apply_suggestions(template: TestTemplate, i: int, suggestions: List[Suggestion]) -> tuple:
    """Save suggested groups: (answers grouped, groups that got answers). Answers already in a
    group or graded by hand are left alone; a suggestion whose key matches an existing group
    (an earlier run) adds to it, and that group must be checked again."""
    uid = template.regions[i].uid
    existing = groups(template, i)
    taken = {m for g in existing for m in g["members"]} | set(hand_graded(template, i))
    grouped = n_groups = 0
    for s in suggestions:
        members = [m for m in s.members if m not in taken]
        if not members:
            continue
        g = next((g for g in existing if g["auto_key"] == s.key or (s.is_blank and g["is_blank"])), None)
        if g is not None:
            set_hand_group_members(g["id"], members, add=True)
            update_hand_group(g["id"], checked=False)
        else:
            add_hand_group(template.id, uid, s.label, members, is_blank=s.is_blank, auto_key=s.key, checked=False)
        taken.update(members)
        grouped += len(members)
        n_groups += 1
    return grouped, n_groups

def suggest_groups(template: TestTemplate, i: int, kind: str, answers: List[Answer],
                   choices: Optional[List[dict]] = None, progress: Callable = None,
                   should_stop: Callable = None) -> Summary:
    """Group the answers of question i that are not grouped or graded yet (kind short / choice).
    Short answers skip the ones the blank CNN marked blank (they go through the Blanks step).
    Nothing is saved when stopped."""
    from .blank_cnn import THRESHOLD
    taken = {m for g in groups(template, i) for m in g["members"]} | set(hand_graded(template, i))
    todo = [a for a in answers if a.sub.id not in taken and a.crop]
    summary = Summary(skipped=len(answers) - len(todo))
    if kind == KIND_CHOICE:
        sugg, summary.unclear = choice_suggestions(template, i, todo, choices or [], progress, should_stop)
    elif kind == KIND_SHORT:
        from ..students import trocr
        reader = trocr.get_reader()
        if reader is None:
            summary.no_reader = True
            return summary
        todo = [a for a in todo if a.p_not_blank is None or a.p_not_blank >= THRESHOLD]
        sugg, summary.unclear = short_suggestions(template, i, todo, reader, progress, should_stop)
    else:
        return summary
    if should_stop and should_stop():
        summary.stopped = True
        return summary
    summary.grouped, summary.groups = apply_suggestions(template, i, sugg)
    return summary
