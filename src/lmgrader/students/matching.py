"""Identify the student who wrote a test.

1. Student ID field (if the template has one): the LLM transcribes the digits; an exact match
   against the roster's student_id is accepted. One wrong digit is tolerated only if exactly one
   roster ID is that close AND the handwritten name agrees with that student.
2. Otherwise (no ID, ID not on the roster): handwritten name -> rapidfuzz -> LLM tiebreak.
3. If the ID and the name point to different students, the match must be confirmed by a human.
"""
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from rapidfuzz import fuzz, process, utils
from rapidfuzz.distance import Levenshtein
from ..models import Student
from ..config import NAME_ACCEPT_SCORE, NAME_ACCEPT_MARGIN, NAME_CONFIRM_SCORE

ID_NAME_AGREE = 60   # fuzzy name score needed to accept an ID that is one digit off

ID_SCHEMA = {
    "type": "object",
    "properties": {"student_id": {"type": "string"}, "legible": {"type": "boolean"}},
    "required": ["student_id", "legible"],
}

@dataclass
class NameMatch:
    student: Optional[Student] = None
    name_read: str = ""
    score: float = 0.0          # fuzzy score of the chosen student vs. what was read
    method: str = ""            # "id", "id~1", "fuzzy", "llm", "none"
    needs_confirm: bool = True  # UI should ask the user to confirm
    id_read: str = ""           # digits read from the student ID field

def normalize_id(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def roster_number(s: Student) -> str:
    """The roster entry's student number, or "" when its student_id is a login such as
    'jsmith2' (mostly letters): only numbers can be compared with a handwritten ID."""
    raw = re.sub(r"\s", "", s.student_id or "")
    digits = normalize_id(raw)
    return digits if len(digits) >= 4 and len(digits) >= 0.6 * len(raw) else ""

def fuzzy_candidates(name: str, students: List[Student], limit: int = 5) -> List[Tuple[Student, float]]:
    """Top roster matches by WRatio (handles reordering, initials, missing middle names)."""
    if not name.strip() or not students:
        return []
    res = process.extract(name, [s.name for s in students], scorer=fuzz.WRatio,
                          processor=utils.default_process, limit=limit)
    return [(students[idx], float(score)) for _, score, idx in res]

def name_score(name: str, student: Student) -> float:
    if not name.strip():
        return 0.0
    return float(fuzz.WRatio(name, student.name, processor=utils.default_process))

def read_student_id(llm, image_b64: str) -> str:
    """Transcribe the handwritten student ID (digits only). Returns '' if blank/illegible."""
    if hasattr(llm, "read_student_id"):          # test doubles / custom clients
        return normalize_id(llm.read_student_id(image_b64))
    from ..llm.client import _image_part, _extract_json
    prompt = ("This image is the 'Student ID' field of an exam. Transcribe the handwritten student "
              "ID number exactly, digits only, no spaces. Ignore printed labels and anything crossed "
              "out. If the field is empty or unreadable, return an empty string. "
              "Respond ONLY as JSON: {\"student_id\": string, \"legible\": boolean}")
    text = llm._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                   _image_part(image_b64)]}], ID_SCHEMA, "student_id")
    d = _extract_json(text) or {}
    return normalize_id(str(d.get("student_id", "")))

def match_name(llm, image_b64: str, students: List[Student]) -> NameMatch:
    """1. LLM transcribes the name.  2. rapidfuzz ranks the roster.
    3. A clear winner is accepted; otherwise the LLM picks among the top candidates
       while looking at the handwriting again."""
    read = llm.read_name(image_b64)
    name = read["name"]
    cands = fuzzy_candidates(name, students)
    if not cands:
        return NameMatch(name_read=name, method="none")
    top_s, top = cands[0]
    second = cands[1][1] if len(cands) > 1 else 0.0
    if top >= NAME_ACCEPT_SCORE and top - second >= NAME_ACCEPT_MARGIN:
        return NameMatch(student=top_s, name_read=name, score=top, method="fuzzy", needs_confirm=False)
    idx = llm.pick_name(image_b64, [s.name for s, _ in cands])
    if idx < 0:
        return NameMatch(student=top_s if top >= NAME_CONFIRM_SCORE else None,
                         name_read=name, score=top, method="none")
    s, sc = cands[idx]
    return NameMatch(student=s, name_read=name, score=sc, method="llm",
                     needs_confirm=sc < NAME_CONFIRM_SCORE)

def match_student(llm, id_b64: Optional[str], name_b64: Optional[str], students: List[Student]) -> NameMatch:
    """ID first, name as fallback / cross-check. Either image may be None."""
    id_read = read_student_id(llm, id_b64) if id_b64 else ""
    by_id = {}
    for s in students:
        sid = roster_number(s)
        if sid:
            by_id.setdefault(sid, []).append(s)

    exact = by_id.get(id_read, []) if len(id_read) >= 4 else []
    if len(exact) == 1:
        st = exact[0]
        name = llm.read_name(name_b64)["name"] if name_b64 else ""
        sc = name_score(name, st)
        # a legible name that clearly belongs to someone else -> human decides
        disagree = bool(name.strip()) and sc < 50 and any(
            c.id != st.id and c_sc >= NAME_ACCEPT_SCORE for c, c_sc in fuzzy_candidates(name, students, 3))
        return NameMatch(student=st, name_read=name, score=100.0, method="id",
                         needs_confirm=disagree, id_read=id_read)

    nm = match_name(llm, name_b64, students) if name_b64 else NameMatch(method="none")
    nm.id_read = id_read
    if len(id_read) >= 4 and by_id:   # a roster without numeric IDs can't confirm or contradict one
        near = [s for sid, ss in by_id.items() if len(sid) == len(id_read)
                and Levenshtein.distance(sid, id_read) <= 1 for s in ss]
        if len(near) == 1 and name_score(nm.name_read, near[0]) >= ID_NAME_AGREE:
            return NameMatch(student=near[0], name_read=nm.name_read, score=name_score(nm.name_read, near[0]),
                             method="id~1", needs_confirm=False, id_read=id_read)
        # ID read but on nobody's roster entry: trust a clear name match only if its ID is close
        # (a 2-digit misread); a far-off ID means ID and name disagree -> human confirms
        if nm.student is not None:
            own = roster_number(nm.student)
            close = bool(own) and Levenshtein.distance(own, id_read) <= 2
            if not (nm.method == "fuzzy" and close):
                nm.needs_confirm = True
    return nm
