"""Local LLM client (LM Studio OpenAI-compatible).

Calls made by the app:
  * extract_question  - template setup, once per region: question image -> LaTeX
  * extract_part      - first part of a question with parts: -> main question (stem) + part
  * generate_rubric   - template setup, once per region: LaTeX question + points -> rubric
  * grade_answer      - per submission/region: answer image + fixed question + fixed rubric
  * read_name / pick_name - per submission: name field image -> roster match
The question text and rubric are stored on the template, so grading never
re-derives them and every student is graded against the same text.
"""
import contextlib
import json
import re
import threading
import time
from typing import Tuple
from openai import OpenAI
from ..config import (DEFAULT_LLM_BASE_URL, DEFAULT_LLM_API_KEY, LLM_CONFIG, clamp_strictness,
                      OPENROUTER_BASE_URL, openrouter_api_key, llm_enabled)


class LLMDisabled(RuntimeError):
    """Settings → "Use an LLM" is off: no LLM client is created."""
    def __init__(self):
        super().__init__("The LLM is turned off in Settings (“Use an LLM”).")

# A failed request is retried until it has failed RETRY_FAILURES times within RETRY_WINDOW_S
# seconds; RETRY_WAIT_S between attempts so a down server gives up quickly instead of hammering.
RETRY_FAILURES = 3
RETRY_WINDOW_S = 60.0
RETRY_WAIT_S = 3.0
# Max seconds the AI gets for one problem; on expiry the grade becomes an 'llm_error' (human review).
GRADE_TIMEOUT_S = 60.0

# Grading strictness (per exam, 5..10): the middle paragraph of the grader's system prompt.
# 10 is the original wording, so exams that never changed the level grade as before.
STRICTNESS_TEXT = {
    10: ("Grade strictly according to the rubric you are given. Award partial credit only where the "
         "rubric allows it."),
    9: ("Follow the rubric closely. Award a criterion only when the student's work clearly shows it. "
        "Within a criterion, you may give part of its points when the work is clearly on the right "
        "track (for example, a correct method with one arithmetic slip)."),
    8: ("Use the rubric as the standard. Take points off for real mathematical errors and missing key "
        "steps. Do not take points off for minor presentation issues (notation, an unsimplified but "
        "equivalent answer, small arithmetic slips) unless the rubric explicitly requires them. "
        "Mention those issues in the feedback."),
    7: ("Use the rubric as a guide to what matters. Give partial credit for a correct method even when "
        "the final answer is wrong. Accept any equivalent form of the answer. Take at most a small "
        "deduction for a minor slip that does not change the idea. Mention every mistake in the "
        "feedback, even when it costs no points."),
    6: ("Focus on whether the student understands the main idea. An answer that is almost correct (one "
        "small slip, notation, a missing final step) gets full or nearly full marks. Any reasonable "
        "attempt earns partial credit. Give zero only for a blank or unrelated answer. Still point out "
        "mistakes in the feedback so the student learns from them."),
    5: ("Be very generous. If the answer is almost correct, give full marks, and never take points off "
        "for technicalities (notation, simplification, small arithmetic slips, missing units). Give "
        "substantial partial credit for any attempt that makes sense; give zero only if the answer is "
        "blank, entirely crossed out, or unrelated to the question. When unsure between two scores, "
        "give the higher one. Still point out mistakes in the feedback so the student learns from them."),
}
STRICTNESS_NAMES = {10: "Strict", 9: "Rubric-exact", 8: "Firm", 7: "Balanced", 6: "Lenient", 5: "Very generous"}

def grader_system(strictness: int = 10) -> str:
    """The grader's system prompt for a strictness level (5 very generous .. 10 strict)."""
    return ("You are a careful, fair teaching assistant grading handwritten exam answers. "
            + STRICTNESS_TEXT[clamp_strictness(strictness)] + " "
            "Never invent work that is not visible in the image. Work the student crossed out is not "
            "part of the answer. If the professor's grading instructions conflict with these guidelines "
            "or with the rubric, follow the professor's instructions.")

GRADER_SYSTEM = grader_system(10)

# The scoring step of the grading request, per strictness level. The system-prompt paragraph
# alone was not enough: told to "apply each rubric criterion and sum the points", local models
# scored every criterion all-or-nothing, so a sensible but imperfect attempt got 0 even at level 5.
# Level 10 keeps the original step (the caller's wording).
_PARTIAL = ("Use the rubric criteria as a guide, not as all-or-nothing checks: a criterion earns part of "
            "its points when the work partly meets it (a correct setup or method with a wrong number, a "
            "right idea with a mistake, a correct conclusion with an incomplete justification). ")
_SCORING_STEP = {
    9: ("Apply each rubric criterion (and the professor's instructions) and sum the points into 'score'. "
        "A criterion may get part of its points when the work is clearly on the right track."),
    8: ("Apply each rubric criterion (and the professor's instructions) and sum the points into 'score'. "
        "Take points off only for real mathematical errors and missing key steps, not for notation, an "
        "unsimplified but equivalent answer or a small arithmetic slip."),
    7: (_PARTIAL + "Add up the points into 'score', following the professor's instructions. Give partial "
        "credit for a correct method even when the final answer is wrong; a sensible attempt rarely "
        "gets 0."),
    6: (_PARTIAL + "Add up the points into 'score', following the professor's instructions. An almost "
        "correct answer gets full or nearly full marks. Any attempt with relevant, sensible work gets "
        "at least a quarter of the maximum points, even if the final answer is wrong. Only a blank, "
        "fully crossed-out or unrelated answer gets 0."),
    5: (_PARTIAL + "Add up the points into 'score', following the professor's instructions. An almost "
        "correct answer gets full marks. Any attempt with relevant, sensible work (a correct setup, a "
        "relevant method or formula, a partly correct computation) gets at least half of the maximum "
        "points, even if the final answer is wrong. Only a blank, fully crossed-out or unrelated answer "
        "gets 0. When unsure between two scores, give the higher one."),
}
_STEP_TAIL = (" The professor's notes for this question override these minimums (for example, a "
              "required method).")

def scoring_step(strictness: int, original: str) -> str:
    """Step 2 of the grading request ('2. ...\\n'). original: the level-10 wording."""
    lvl = clamp_strictness(strictness)
    if lvl == 10:
        return original
    tail = _STEP_TAIL if lvl <= 6 else ""
    return (f"2. Grading standard: {STRICTNESS_NAMES[lvl].lower()} (set by the professor). "
            f"{_SCORING_STEP[lvl]}{tail}\n")

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "transcription": {"type": "string"},
        "score": {"type": "number"},
        "feedback": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
        "review_reason": {"type": "string"},
        "confidence": {"type": "number"},
        "continues_elsewhere": {"type": "boolean"},
    },
    "required": ["transcription", "score", "feedback", "needs_human_review", "review_reason",
                 "confidence", "continues_elsewhere"],
}

def _instructions_block(instructions: str = "", notes: str = "") -> str:
    """Professor's grading instructions (exam-wide) and notes for this question."""
    out = ""
    if instructions.strip():
        out += f"Professor's grading instructions (apply to every question):\n{instructions.strip()}\n\n"
    if notes.strip():
        out += f"Professor's notes for this question:\n{notes.strip()}\n\n"
    return out

CROSSED_OUT_NOTE = (
    "Crossed-out work: students cross out what they do not want graded (struck through, scribbled "
    "over, X'd out, or boxed with a line through it). Ignore it completely: do not transcribe it, do "
    "not give or take points for it, and do not mention it in the feedback. Grade only the work that "
    "is not crossed out. If everything is crossed out, treat the answer as blank. If you cannot tell "
    "whether something is crossed out, set needs_human_review true and say so in review_reason.\n\n")

PARTS_NOTE = ("If the question lists a main question and earlier parts, they are context only: grade "
              "only the part named 'Part to grade'.\n\n")

RUBRIC_PARTS_NOTE = ("If the question lists a main question and earlier parts, use them to understand "
                     "the setup (the given matrix, function, data...), but write the rubric ONLY for the "
                     "part named 'Part to grade': no criteria for the earlier parts. If this part uses "
                     "a result of an earlier part, the expected answer uses the correct value of that "
                     "result.\n\n")

# a part with later parts also leaves a note for their graders (they only see their own box)
GRADE_SCHEMA_NEXT = {
    "type": "object",
    "properties": {**GRADE_SCHEMA["properties"], "next_part_notes": {"type": "string"}},
    "required": GRADE_SCHEMA["required"] + ["next_part_notes"],
}

NEXT_PART_STEP = (
    "6. 'next_part_notes': a short note for the grader of the LATER parts of this question, who "
    "cannot see this answer. State the student's final result for this part, whether it is "
    "correct, and if it is wrong the correct result, so later graders can tell when an error is "
    "carried forward. Empty string if there is nothing useful.\n")

def _prior_notes_block(prior_notes: str = "") -> str:
    """Notes left by the graders of the earlier parts, so an error is not penalized twice."""
    if not prior_notes.strip():
        return ""
    return ("Notes from the graders of the earlier parts of this question (about THIS student's work):\n"
            f"{prior_notes.strip()}\n"
            "If this part reuses a wrong result from an earlier part, that error was already penalized "
            "there: do not take points off again for it. Grade this part on whether the work is correct "
            "given the student's own earlier result (follow-through), unless the professor's "
            "instructions say otherwise, and say in the feedback that the earlier error was carried "
            "forward.\n\n")

RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "expected_answer": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, "points": {"type": "number"}},
                "required": ["description", "points"],
            },
        },
    },
    "required": ["expected_answer", "criteria"],
}

NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "legible": {"type": "boolean"}},
    "required": ["name", "legible"],
}

_BOX = {"type": "array", "items": {"type": "number"}}
LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "question_box": _BOX,
                    "answer_box": _BOX,
                    "points": {"type": ["number", "null"]},
                },
                "required": ["label", "question_box", "answer_box", "points"],
            },
        },
        "name_box": {"type": ["array", "null"], "items": {"type": "number"}},
        "id_box": {"type": ["array", "null"], "items": {"type": "number"}},
    },
    "required": ["questions", "name_box", "id_box"],
}

TRANSCRIBE_MAX_TOKENS = 1200   # a question is short; a longer reply means the model is looping

_MAIN_HEAD = re.compile(r"^\s*\**\s*MAIN\s+QUESTION\s*\**\s*:\s*\**", re.I | re.M)
_PART_HEAD = re.compile(r"^\s*\**\s*PART\s*(?:\(\s*[a-z]{1,4}\s*\)|[a-z]{1,4})?\s*\**\s*:\s*\**", re.I | re.M)

def split_part_reply(text: str, letter: str = "") -> dict:
    """Model reply to extract_part -> {"stem", "part"}. Expected format:
        MAIN QUESTION:\\n<stem or NONE>\\nPART (a):\\n<part>
    Also accepts a JSON {"stem", "part"} reply (older prompt). Never returns raw JSON or the
    headings as text: if there is no stem/part split, the whole reply is the part."""
    text = _strip_fences(_strip_reasoning(text or "")).strip()
    if text.startswith("{"):
        d = _extract_json(text)
        if isinstance(d, dict) and ("stem" in d or "part" in d):
            stem, part = str(d.get("stem") or "").strip(), str(d.get("part") or "").strip()
            if not part:
                stem, part = "", stem
            return {"stem": repair_latex_text(stem), "part": repair_latex_text(part)}
    m_main, m_part = _MAIN_HEAD.search(text), _PART_HEAD.search(text)
    if m_part and (m_main is None or m_main.start() < m_part.start()):
        stem = text[m_main.end():m_part.start()].strip() if m_main else ""
        part = text[m_part.end():].strip()
    elif m_main:
        stem, part = "", text[m_main.end():].strip()
    else:
        stem, part = "", text
    if re.fullmatch(r"\(?\s*(none|n/?a|-+)?\s*\)?\.?", stem, re.I):
        stem = ""
    part = re.sub(r"^\s*\(?\s*(none|n/a)\s*\)?\.?\s*\n", "", part, flags=re.I)   # stray "NONE" line
    if not part:
        stem, part = "", stem
    return {"stem": stem, "part": strip_part_label(part, letter)}

def strip_part_label(text: str, letter: str = "") -> str:
    """'(b) Find ...' -> 'Find ...' (the part's own label is not part of its text)."""
    lab = re.escape(letter) if letter else r"[a-z]"
    return re.sub(rf"^\s*\(\s*{lab}\s*\)\s*", "", text or "", flags=re.I)

PICK_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "integer"}},
    "required": ["choice"],
}

def _strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    if "</think>" in text:  # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
    return text.strip()

def _strip_fences(text: str) -> str:
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", text.strip(), re.DOTALL)
    return m.group(1).strip() if m else text.strip()

# LaTeX commands that start with a letter JSON treats as an escape (\n, \r, \t): "\neq" in a model's
# JSON reply is the LaTeX command, not a newline followed by "eq". (\b and \f + letters are always
# LaTeX: \begin, \frac, ...; any other backslash + letter is not a JSON escape at all.)
_LATEX_NRT = {
    "ne", "neq", "nabla", "neg", "nu", "not", "notin", "newline", "nmid", "nexists", "ni", "nleq",
    "ngeq", "nless", "ngtr", "nsubseteq", "nsupseteq", "nparallel", "nrightarrow", "nleftarrow",
    "normalsize", "nolimits",
    "right", "rightarrow", "rightleftharpoons", "rho", "rangle", "rceil", "rfloor", "rm", "rvert",
    "rVert", "rbrace", "rbrack", "root", "rbrace", "restriction",
    "text", "textbf", "textit", "textrm", "textsf", "texttt", "textstyle", "textup", "times", "theta",
    "to", "tan", "tanh", "tau", "tfrac", "top", "tilde", "therefore", "triangle", "triangleq",
    "tt", "tbinom", "tiny", "trace", "tr", "transpose",
}

def _fix_latex_escapes(raw: str) -> str:
    """Make a model's JSON safe to decode when its strings contain LaTeX written with single
    backslashes ("\\begin", "\\frac", "\\sqrt"): those backslashes are doubled so json.loads
    keeps them, instead of turning "\\b"/"\\t" into control characters (\\begin -> 'egin') or
    failing on "\\s". Real JSON escapes (\\n newline, \\", \\uXXXX) are kept. A doubled
    backslash is ambiguous: in a reply that does not escape its LaTeX (some single-backslash
    command had to be fixed) it is a LaTeX row break and is kept as two backslashes."""
    def run(literal_double: bool):
        out, i, n, fixed = [], 0, len(raw), 0
        while i < n:
            c = raw[i]
            if c != "\\" or i + 1 >= n:
                out.append(c); i += 1
                continue
            nxt = raw[i + 1]
            if nxt == "\\":
                out.append("\\\\\\\\" if literal_double else "\\\\"); i += 2
                continue
            if nxt in '"/':
                out.append(raw[i:i + 2]); i += 2
                continue
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[i + 2:i + 6] or ""):
                out.append(raw[i:i + 6]); i += 6
                continue
            word = re.match(r"[A-Za-z]+", raw[i + 1:])
            word = word.group(0) if word else ""
            if nxt in "bf" and len(word) > 1 or nxt in "nrt" and word in _LATEX_NRT:
                out.append("\\\\"); i += 1; fixed += 1      # LaTeX command: escape the backslash
            elif nxt in "bfnrt":
                out.append(raw[i:i + 2]); i += 2            # a real JSON escape
            else:
                out.append("\\\\"); i += 1; fixed += 1      # invalid JSON escape (\sqrt, \pi, ...)
        return "".join(out), fixed
    text, fixed = run(False)
    return run(True)[0] if fixed else text

def _extract_json(text: str):
    text = _strip_reasoning(text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    for candidate in (_fix_latex_escapes(m.group(0)), m.group(0)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None

def repair_latex_text(text: str) -> str:
    """Undo LaTeX damaged by JSON decoding in stored text: backspace/form feed + letters were
    '\\b', '\\f' commands ('\\x08egin' -> '\\begin'); tab/CR + a known command were '\\t', '\\r'
    commands ('\\ttext'). Newlines are left alone (a real line break + 'e.g.' is not '\\ne')."""
    if not text:
        return text
    text = re.sub(r"\x08(?=[A-Za-z])", lambda m: "\\b", text)
    text = re.sub(r"\x0c(?=[A-Za-z])", lambda m: "\\f", text)
    for ctrl, letter in (("\t", "t"), ("\r", "r")):
        text = re.sub(re.escape(ctrl) + r"([A-Za-z]+)",
                      lambda m, l=letter: "\\" + l + m.group(1) if l + m.group(1) in _LATEX_NRT else m.group(0),
                      text)
    return text

def is_repetitive(text: str, window: int = 40, times: int = 3) -> bool:
    """True if the model got stuck repeating itself (some 40-character stretch occurs 3+ times)."""
    t = re.sub(r"\s+", " ", text or "")
    if len(t) < window * times:
        return False
    return any(t.count(t[k:k + window]) >= times for k in range(0, len(t) - window, 20))

def cut_repetition(text: str, window: int = 40) -> str:
    """Keep the text up to where it starts repeating itself."""
    t = re.sub(r"[ \t]+", " ", text or "")
    for k in range(0, max(0, len(t) - window)):
        again = t.find(t[k:k + window], k + window)
        if again != -1:
            return t[:again].rstrip()
    return t

# ---- grading replies: keep them short, and recover when the model loops ----
FEEDBACK_LIMIT = "(at most 3 sentences; say each thing once)"
LOOP_RETRY_NOTE = ("\n\nIMPORTANT: a previous answer to this repeated the same sentences over and over until "
                   "it was cut off. Write every field exactly once, keep 'feedback' to at most 3 sentences, "
                   "never repeat a sentence, and close the JSON.")

def _degenerate_grade(text: str) -> bool:
    """The reply is cut off / not JSON, or the model got stuck repeating itself."""
    d = _extract_json(text)
    return d is None or any(is_repetitive(str(d.get(k, ""))) for k in ("feedback", "transcription"))

def run_grading(llm, messages, schema, max_points: float) -> dict:
    """Grading request; a looping or cut-off reply is asked again once with a strict note. If it
    loops again, _parse keeps what is usable (score, text up to the repetition) and flags it."""
    budget = getattr(llm, "time_budget", None)
    with (budget(GRADE_TIMEOUT_S) if budget else contextlib.nullcontext()):   # whole problem (retries included); expiry -> TimeoutError
        text = llm._chat(messages, schema, "grade")
        if _degenerate_grade(text):
            retry = [dict(m) for m in messages]
            content = list(retry[-1]["content"])
            content[0] = {"type": "text", "text": content[0]["text"] + LOOP_RETRY_NOTE}
            retry[-1]["content"] = content
            again = llm._chat(retry, schema, "grade")
            if not _degenerate_grade(again) or _extract_json(text) is None:
                text = again
    return llm._parse(text, max_points)

def _salvage_grade_json(text: str):
    """Fields of a grading reply that was cut off before the JSON closed (the model looped until
    max_tokens). None unless at least the score is there."""
    t = _strip_reasoning(text or "")
    m = re.search(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', t)
    if not m:
        return None
    d = {"score": float(m.group(1))}
    for key in ("transcription", "feedback", "review_reason", "next_part_notes"):
        s = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % key, t, re.DOTALL)
        if s:
            raw = s.group(1).rstrip("\\")
            try:
                d[key] = json.loads(_fix_latex_escapes('{"v": "' + raw + '"}'))["v"]
            except (json.JSONDecodeError, KeyError):
                d[key] = raw
    for key in ("needs_human_review", "continues_elsewhere"):
        b = re.search(r'"%s"\s*:\s*(true|false)' % key, t)
        if b:
            d[key] = b.group(1) == "true"
    c = re.search(r'"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', t)
    if c:
        d["confidence"] = float(c.group(1))
    return d

def _image_part(image_b64: str, mime="image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}}

class LLMClient:
    _deadline = None     # monotonic time the current time_budget ends

    def __init__(self, base_url=DEFAULT_LLM_BASE_URL, api_key=DEFAULT_LLM_API_KEY, model=None,
                 max_tokens=2048, temperature=0.1):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model or "local-model"
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self._client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)  # retries: _chat
        self._schema_supported = True
        self._aborted = threading.Event()

    @contextlib.contextmanager
    def time_budget(self, seconds: float):
        """Everything requested inside (retries too) must finish within `seconds`, else TimeoutError."""
        prev, self._deadline = self._deadline, time.monotonic() + seconds
        try:
            yield
        finally:
            self._deadline = prev

    def _remaining(self):
        if self._deadline is None:
            return None
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the AI took too long on this problem")
        return left

    @classmethod
    def from_config(cls) -> "LLMClient":
        if not llm_enabled():
            raise LLMDisabled()
        return cls(base_url=LLM_CONFIG["base_url"], api_key=LLM_CONFIG["api_key"],
                   model=LLM_CONFIG["model"], max_tokens=LLM_CONFIG.get("max_tokens", 2048),
                   temperature=LLM_CONFIG.get("temperature", 0.1))

    @classmethod
    def for_grading(cls) -> "LLMClient":
        """Client used to grade answers: the local server, or OpenRouter when selected in Settings
        (key from OPENROUTER_API_KEY in the environment / .env). Name reading etc. use from_config().
        While config.OPENROUTER_ENABLED is False it is always the local server."""
        from .. import config
        if not llm_enabled():
            raise LLMDisabled()
        if not config.OPENROUTER_ENABLED or LLM_CONFIG.get("grading_provider") != "openrouter":
            return cls.from_config()
        key = openrouter_api_key()
        if not key:
            raise RuntimeError("OpenRouter grading is selected but OPENROUTER_API_KEY was not found "
                               "(set it in a .env file or the environment).")
        return cls(base_url=OPENROUTER_BASE_URL, api_key=key,
                   model=LLM_CONFIG.get("openrouter_model") or "qwen/qwen3-vl-8b-instruct",
                   max_tokens=LLM_CONFIG.get("max_tokens", 2048),
                   temperature=LLM_CONFIG.get("temperature", 0.1))

    def set_model(self, model):
        self.model = model

    def abort(self):
        """Abort any in-flight request (used by Stop buttons). The client is unusable afterwards."""
        self._aborted.set()   # no retries after a Stop
        self._client.close()

    def list_models(self) -> list:
        try:
            return [m.id for m in self._client.models.list().data]
        except Exception:
            return []

    def reachable(self, timeout: float = 3.0) -> bool:
        """Quick check that the server answers at all (no retries), e.g. before asking it about
        every test of a batch."""
        try:
            self._client.with_options(timeout=timeout).models.list()
            return True
        except Exception:
            return False

    # ---- low level ----
    def _chat(self, messages, schema=None, schema_name="response", max_tokens=None, temperature=None) -> str:
        """One request, retried on errors until it fails RETRY_FAILURES times within RETRY_WINDOW_S
        (then the last error is raised). A Stop (abort) ends the retries at once."""
        failures = []
        while True:
            try:
                self._remaining()
                return self._chat_once(messages, schema, schema_name, max_tokens, temperature)
            except Exception as e:
                if self._aborted.is_set():
                    raise
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    raise TimeoutError("the AI took too long on this problem") from e
                now = time.monotonic()
                failures = [t for t in failures if now - t < RETRY_WINDOW_S] + [now]
                if len(failures) >= RETRY_FAILURES:
                    raise
                if self._aborted.wait(RETRY_WAIT_S):
                    raise

    def _create(self, **kwargs):
        """The HTTP call. Under a time_budget it runs in a helper thread so the budget is wall-clock:
        the SDK's timeout only limits the gap between bytes, and keep-alive bytes would reset it."""
        create = self._client.chat.completions.create
        left = self._remaining()
        if left is None:
            return create(**kwargs)
        box = {}
        def call():
            try:
                box["reply"] = create(timeout=left, **kwargs)
            except Exception as e:
                box["error"] = e
        t = threading.Thread(target=call, daemon=True)
        t.start()
        while t.is_alive():
            if self._aborted.is_set():
                raise RuntimeError("stopped")
            self._remaining()     # raises TimeoutError once the budget is spent
            t.join(0.1)
        if "error" in box:
            raise box["error"]
        return box["reply"]

    def _chat_once(self, messages, schema=None, schema_name="response", max_tokens=None, temperature=None) -> str:
        kwargs = dict(model=self.model, messages=messages,
                      temperature=self.temperature if temperature is None else temperature,
                      max_tokens=min(self.max_tokens, max_tokens) if max_tokens else self.max_tokens)
        if schema is not None and self._schema_supported:
            try:
                resp = self._create(
                    response_format={"type": "json_schema",
                                     "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
                    **kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:
                # older servers / models without structured output: fall back to prompt-only JSON
                if "response_format" not in str(e) and "json_schema" not in str(e):
                    raise
                self._schema_supported = False
        resp = self._create(**kwargs)
        return resp.choices[0].message.content or ""

    # ---- template setup (run once, result is stored on the template) ----
    def extract_question(self, image_b64: str, part_of: Tuple[str, str] = None) -> str:
        """Stage 1: transcribe the printed question in the image as LaTeX.
        part_of=('1', 'b'): the crop is a later part of question 1; only part (b) is transcribed
        (the main question's text is read once, with the first part, by extract_part)."""
        part = ""
        if part_of:
            num, letter = part_of
            part = (f"This crop shows part ({letter}) of question {num}. Transcribe only the text of "
                    f"part ({letter}), without its label; if the main question's shared text or other "
                    "parts are visible, leave them out. ")
        prompt = (
            "This image is a cropped region of a printed exam page containing one question. "
            + part +
            "Transcribe the question text exactly, using LaTeX for all mathematics "
            "(inline math in $...$, display math in \\[...\\]). "
            "Include every given value and instruction. If there is a figure, "
            "describe it briefly in [Figure: ...]. Do NOT solve the question. "
            "Output only the LaTeX transcription, once: no preamble, no notes or comments, no code fences."
        )
        text = self._transcribe(prompt, image_b64)
        if part_of:
            return strip_part_label(text, part_of[1])
        return re.sub(r"^\s*\d{1,2}\s*\.\s+(?=\S)", "", text)    # "5. Use Cramer's ..." -> "Use ..."

    def _transcribe(self, prompt: str, image_b64: str) -> str:
        """Plain-text transcription (no JSON: LaTeX backslashes stay intact). If the model gets
        stuck repeating itself, retry once more strictly; if it still loops, keep the text up to
        where the repetition starts (the professor reviews every question anyway)."""
        msg = lambda p: [{"role": "user", "content": [{"type": "text", "text": p}, _image_part(image_b64)]}]
        text = _strip_fences(_strip_reasoning(self._chat(msg(prompt), max_tokens=TRANSCRIBE_MAX_TOKENS)))
        if is_repetitive(text):
            retry = prompt + ("\nIMPORTANT: write the transcription exactly once. Do not revise, repeat, "
                              "comment on or explain it.")
            text = _strip_fences(_strip_reasoning(self._chat(msg(retry), max_tokens=TRANSCRIBE_MAX_TOKENS,
                                                             temperature=0.0)))
            if is_repetitive(text):
                text = cut_repetition(text)
        return text.strip()

    def extract_part(self, image_b64: str, number: str, letter: str) -> dict:
        """For the FIRST part (letter) of question (number), whose crop also shows the main
        question's shared text: returns {"stem": main question text ('' if none), "part": text of
        this part}. The stem is stored once and sent with every part. Later parts use
        extract_question(part_of=...) and are never asked for a stem."""
        # plain text with two headings, not JSON: LaTeX inside JSON strings loses its backslashes
        # (\begin -> backspace + 'egin') and the JSON format made the model ramble inside the field
        prompt = (
            f"This image is a cropped region of a printed exam page. It shows the beginning of "
            f"question {number}: the main question's shared text (the setup that applies to all of "
            f"its parts, if there is one) followed by part ({letter}).\n"
            "Transcribe exactly, using LaTeX for all mathematics (inline math in $...$, display math "
            "in \\[...\\]). Include every given value and instruction. Describe any figure briefly "
            "in [Figure: ...]. Do NOT solve anything. Write each text once: no notes or comments.\n"
            "Reply in exactly this format and nothing else:\n"
            "MAIN QUESTION:\n"
            f"<the shared text before part ({letter}), or NONE if the question starts directly with "
            f"part ({letter})>\n"
            f"PART ({letter}):\n"
            f"<the text of part ({letter}) only, without its label>"
        )
        return split_part_reply(self._transcribe(prompt, image_b64), letter)

    def generate_rubric(self, question_latex: str, max_points: float, problem: str = "",
                        instructions: str = "", notes: str = "", answer_key: str = ""):
        """Create a structured rubric (expected answer + items with points, add-up scoring) for a
        fixed question; returns a models.Rubric. The professor's instructions/notes shape the
        criteria (e.g. points for shown work). answer_key: the professor's own answer (imported
        answer key); used instead of solving."""
        key = (f"The professor's answer key for this question:\n{answer_key.strip()}\n"
               "This answer is authoritative: use it as expected_answer (you may restate it "
               "concisely) and do not contradict it.\n\n") if answer_key.strip() else ""
        given = [w for w, s in (("instructions", instructions), ("notes for this question", notes)) if s.strip()]
        # the professor's words were only context before; the rules below must not overrule them
        follow = (f"The professor's {' and '.join(given)} above come first: write the criteria so they "
                  "follow them (what to accept, what earns or loses points, partial credit), even where "
                  "they differ from the other rules in this request.\n") if given else ""
        prompt = (
            f"Write a grading rubric for exam problem {problem or ''}.\n\n"
            f"Question (LaTeX):\n{question_latex}\n\n"
            f"Total points: {max_points:g}.\n\n"
            + _instructions_block(instructions, notes) + RUBRIC_PARTS_NOTE + key +
            ("Use the answer key above as the expected final answer. " if key else
             "First solve the problem and state the expected final answer concisely. ") +
            "Then list 2-6 gradable criteria (setup, key steps, final answer, ...) whose points "
            f"add up to exactly {max_points:g}. Each criterion must be checkable from a student's "
            "handwritten work.\n"
            "True/false questions get NO partial credit: write exactly one criterion, worth all "
            f"{max_points:g} points, for the correct answer (with its justification, if the question "
            "asks for one); the student gets all of it or nothing.\n"
            + follow +
            "Respond ONLY as JSON: {\"expected_answer\": string, "
            "\"criteria\": [{\"description\": string, \"points\": number}]}"
        )
        text = self._chat([{"role": "system", "content": GRADER_SYSTEM},
                           {"role": "user", "content": prompt}], RUBRIC_SCHEMA, "rubric")
        d = _extract_json(text)
        if not d or not d.get("criteria"):
            raise ValueError(f"Could not parse rubric from model output:\n{_strip_reasoning(text)[:500]}")
        return rubric_from_reply(d, max_points)

    def detect_layout(self, image_b64: str, width: int, height: int,
                      sent_w: int = None, sent_h: int = None, columns: int = 0) -> dict:
        """Locate questions, answer spaces, the name field and the student-ID field on a blank
        exam page. width/height: original page size (output coords); sent_w/sent_h: size of the
        image actually sent (only needed if the model answers in pixels instead of 0-1000).
        columns: how many columns the questions are laid out in (0 = unknown).
        Returns {"questions": [{problem, question_rect, answer_rect, points}], "name_rect",
        "id_rect", "columns"} with questions in reading order. Run template.layout.refine_layout
        on the result to snap boxes to the printed rectangles."""
        col_hint = (f"The questions are laid out in {columns} columns; read each column top to "
                    "bottom before moving to the next column.\n") if columns and columns > 1 else (
                    "The page may use more than one column of questions.\n")
        prompt = (
            "This is a blank exam page. Locate every question and every sub-part on it.\n"
            + col_hint +
            "Make ONE entry per gradable part: if question 1 has parts (a) and (b), give two entries "
            "labelled \"1a\" and \"1b\" (parent number + part letter); a question without parts is "
            "labelled just with its number, e.g. \"5\". True/false or multiple-choice items with "
            "parts (a)-(f) also get one entry per part (\"9a\" ... \"9f\").\n"
            "For each entry give:\n"
            "- label: as described above\n"
            "- question_box: box around the printed text of that part\n"
            "- answer_box: box around where the student answers: the drawn rectangle under that "
            "part, or for true/false the bubbles of that part, or else the blank space below it\n"
            "- points: the points printed for that part, or null if none are printed\n"
            "Also give name_box: the rectangle/line where the student writes their NAME, and id_box: "
            "the rectangle/line where they write their STUDENT ID number (null if absent). Do not list "
            "the name or ID fields as questions.\n"
            "Boxes are [x1, y1, x2, y2] with coordinates normalized to 0-1000 relative to the "
            "image width and height (0,0 = top-left, 1000,1000 = bottom-right).\n"
            "Respond ONLY as JSON: {\"questions\": [{\"label\": string, \"question_box\": [x1,y1,x2,y2], "
            "\"answer_box\": [x1,y1,x2,y2], \"points\": number|null}], \"name_box\": [x1,y1,x2,y2]|null, "
            "\"id_box\": [x1,y1,x2,y2]|null}"
        )
        text = self._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        _image_part(image_b64)]}], LAYOUT_SCHEMA, "layout")
        d = _extract_json(text)
        if d is None:
            raise ValueError(f"Could not parse layout from model output:\n{_strip_reasoning(text)[:500]}")
        return parse_layout(d, width, height, sent_w or width, sent_h or height, columns)

    # ---- grading ----
    def grade_answer(self, image_b64: str, problem: str, group: str, rubric: str,
                     max_points: float, question: str = "", mime="image/png",
                     instructions: str = "", notes: str = "", prior_notes: str = "",
                     has_next: bool = False, strictness: int = 10) -> dict:
        """Send cropped answer image to vision model.
        prior_notes: notes left by the graders of earlier parts; has_next: later parts exist, so
        the model also writes next_part_notes for them. strictness: 5 (very generous) .. 10 (strict).
        Returns {transcription, score, feedback, needs_human_review, review_reason,
        confidence (0-1), continues_elsewhere, next_part_notes}."""
        rubric = rubric_text(rubric, max_points)
        prompt = (
            f"Problem {problem} (section '{group}'). Maximum points: {max_points:g}.\n\n"
            f"Question:\n{question or '(question text not provided; infer it from the image)'}\n\n"
            f"Rubric:\n{rubric or 'Grade based on correctness and completeness.'}\n\n"
            + _instructions_block(instructions, notes) + PARTS_NOTE + _prior_notes_block(prior_notes) +
            CROSSED_OUT_NOTE +
            "The image shows the student's answer box. Steps:\n"
            "1. Transcribe exactly what the student wrote (LaTeX for math) into 'transcription', "
            "leaving out crossed-out work.\n"
            + scoring_step(strictness, "2. Apply each rubric criterion (and the professor's instructions) "
                                       "to the transcription and sum the points into 'score'.\n") +
            "3. Give the student short, specific 'feedback' naming which criteria were met or missed "
            + FEEDBACK_LIMIT + ".\n"
            "4. 'confidence': a number from 0 to 1 for how sure you are that the score is right "
            "(below 0.7 if any part is hard to read or the grading call is a judgment call).\n"
            "5. 'continues_elsewhere': true if the student indicates the work continues somewhere "
            "else (e.g. 'see back', 'continued on next page', an arrow off the box, 'ver atrás').\n"
            + (NEXT_PART_STEP if has_next else "") +
            "Set needs_human_review true (and explain in review_reason) if the writing is illegible, "
            "the answer is ambiguous, the rubric does not cover the student's approach, or the work "
            "continues elsewhere.\n"
            "Respond ONLY as JSON with keys: transcription, score, feedback, needs_human_review, "
            "review_reason, confidence, continues_elsewhere" + (", next_part_notes." if has_next else ".")
        )
        try:
            return run_grading(self, [{"role": "system", "content": grader_system(strictness)},
                                      {"role": "user", "content": [{"type": "text", "text": prompt},
                                                                   _image_part(image_b64, mime)]}],
                               GRADE_SCHEMA_NEXT if has_next else GRADE_SCHEMA, max_points)
        except Exception as e:
            return {"score": 0.0, "feedback": f"LLM error: {e}", "needs_human_review": True,
                    "review_reason": "llm_error", "transcription": "", "confidence": 0.0,
                    "continues_elsewhere": False, "next_part_notes": ""}

    def _parse(self, text: str, max_points: float) -> dict:
        d = _extract_json(text)
        salvaged = d is None and (d := _salvage_grade_json(text)) is not None
        if d is not None:
            try:
                score = max(0.0, min(float(d.get("score", 0)), max_points))
                try:
                    conf = float(d.get("confidence", 1.0))
                    conf = conf / 100 if conf > 1 else conf   # tolerate percentages
                except (TypeError, ValueError):
                    conf = 0.0
                res = {
                    "score": score,
                    "feedback": str(d.get("feedback", "")),
                    "needs_human_review": bool(d.get("needs_human_review", False)),
                    "review_reason": str(d.get("review_reason", "")),
                    "transcription": str(d.get("transcription", "")),
                    "confidence": max(0.0, min(conf, 1.0)),
                    "continues_elsewhere": bool(d.get("continues_elsewhere", False)),
                    "next_part_notes": str(d.get("next_part_notes") or "").strip(),
                }
                # a looping model: keep each field up to where it starts repeating; a human decides
                looped = [k for k in ("feedback", "transcription") if is_repetitive(res[k])]
                for k in looped:
                    res[k] = cut_repetition(res[k])
                if salvaged or looped:
                    why = "truncated_response (model repeated itself)" if salvaged else "repetitive_output"
                    res["needs_human_review"], res["confidence"] = True, 0.0
                    res["review_reason"] = "; ".join(r for r in (res["review_reason"], why) if r)
                return res
            except (TypeError, ValueError):
                pass
        return {"score": 0.0, "feedback": _strip_reasoning(text), "needs_human_review": True,
                "review_reason": "unparsed_response", "transcription": "", "confidence": 0.0,
                "continues_elsewhere": False, "next_part_notes": ""}

    # ---- student identification ----
    def read_name(self, image_b64: str) -> dict:
        """Transcribe the handwritten name. Returns {name, legible}."""
        prompt = ("This image is the name field of an exam. Transcribe the student's handwritten "
                  "name exactly as written (ignore printed labels like 'Name:'). "
                  "Respond ONLY as JSON: {\"name\": string, \"legible\": boolean}")
        text = self._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        _image_part(image_b64)]}], NAME_SCHEMA, "name")
        d = _extract_json(text) or {}
        return {"name": str(d.get("name", "")).strip(), "legible": bool(d.get("legible", False))}

    def pick_name(self, image_b64: str, candidates: list) -> int:
        """Ask the model which roster candidate matches the handwriting. Returns index or -1."""
        listing = "\n".join(f"{i}: {c}" for i, c in enumerate(candidates))
        prompt = ("This image is the handwritten name field of an exam. Which of these students "
                  f"wrote it?\n{listing}\n"
                  "Answer with the number of the matching student, or -1 if none match. "
                  "Respond ONLY as JSON: {\"choice\": integer}")
        text = self._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        _image_part(image_b64)]}], PICK_SCHEMA, "pick")
        d = _extract_json(text) or {}
        try:
            i = int(d.get("choice", -1))
        except (TypeError, ValueError):
            return -1
        return i if 0 <= i < len(candidates) else -1

    def read_version(self, image_b64: str, labels: list) -> str:
        """Read which exam version the version field shows. Returns one of `labels`, or "" if
        the model can't tell."""
        listing = ", ".join(f'"{l}"' for l in labels)
        schema = {"type": "object", "properties": {"version": {"type": "string", "enum": list(labels) + ["unknown"]}},
                  "required": ["version"]}
        prompt = ("This image is the part of an exam page that says which version (form) of the exam it is: "
                  "printed text such as 'Version B' / 'Form 2', or a letter the student circled, checked or "
                  f"wrote. The possible versions are: {listing}. Which one does it show? Answer \"unknown\" "
                  "if it is blank, unreadable, or several are marked. "
                  "Respond ONLY as JSON: {\"version\": string}")
        text = self._chat([{"role": "user", "content": [{"type": "text", "text": prompt},
                                                        _image_part(image_b64)]}], schema, "version")
        return match_version_label(str((_extract_json(text) or {}).get("version", "")), labels)

def match_version_label(text: str, labels: list) -> str:
    """'Version b', 'FORM B', 'b' -> 'B' when 'B' is a label; "" if no single label matches."""
    norm = lambda s: re.sub(r"(?i)\b(version|versi[oó]n|form|exam|test)\b|[^0-9A-Za-zÀ-ÿ]", "", s).upper()
    t = norm(text)
    hits = [l for l in labels if t and norm(l) == t]
    return hits[0] if len(hits) == 1 else ""

def _box_values(b):
    if isinstance(b, dict):  # {"x1":..,"y1":..} style
        b = [b.get(k) for k in ("x1", "y1", "x2", "y2")]
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    try:
        return [float(v) for v in b]
    except (TypeError, ValueError):
        return None

def parse_layout(d: dict, width: int, height: int, sent_w: int, sent_h: int, columns: int = 0) -> dict:
    """Convert model boxes to pixel (x, y, w, h) rects in the original page, in reading order.
    Boxes are expected normalized to 0-1000; if any coordinate exceeds 1000 the model
    evidently answered in pixels of the image it was sent."""
    from ..template.layout import order_questions
    qs = d.get("questions") or []
    boxes = [_box_values(q.get(k)) for q in qs if isinstance(q, dict)
             for k in ("question_box", "answer_box")] + [_box_values(d.get("name_box")),
                                                          _box_values(d.get("id_box"))]
    vals = [v for b in boxes if b for v in b]
    pixels = bool(vals) and max(vals) > 1000
    sx = width / (sent_w if pixels else 1000.0)
    sy = height / (sent_h if pixels else 1000.0)

    def rect(b):
        b = _box_values(b)
        if b is None:
            return None
        x1, y1, x2, y2 = b
        x1, x2 = sorted((x1 * sx, x2 * sx))
        y1, y2 = sorted((y1 * sy, y2 * sy))
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(width, int(x2)), min(height, int(y2))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        return (x1, y1, x2 - x1, y2 - y1)

    out = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        pts = q.get("points")
        try:
            pts = float(pts) if pts is not None else None
        except (TypeError, ValueError):
            pts = None
        item = {"problem": str(q.get("label") or "").strip(),
                "question_rect": rect(q.get("question_box")),
                "answer_rect": rect(q.get("answer_box")),
                "points": pts}
        if item["question_rect"] or item["answer_rect"]:
            out.append(item)
    return {"questions": order_questions(out, width, columns), "name_rect": rect(d.get("name_box")),
            "id_rect": rect(d.get("id_box")), "columns": int(columns or 0)}

def downscale_for_llm(img, max_side: int = 1280):
    """Returns (resized image, sent_w, sent_h)."""
    import cv2
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1.0:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img, img.shape[1], img.shape[0]

def rubric_text(rubric, max_points: float) -> str:
    """The rubric as prompt text: a models.Rubric is rendered, text is passed through."""
    return rubric.as_text(max_points) if hasattr(rubric, "as_text") else (rubric or "")

def rubric_from_reply(d: dict, max_points: float):
    """{"expected_answer", "criteria": [{"description", "points"}]} -> models.Rubric (add-up)."""
    from ..models import Rubric, RubricItem
    items = []
    for c in d.get("criteria", []) or []:
        try:
            pts = float(c.get("points", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        desc = str(c.get("description", "")).strip()
        if desc:
            items.append(RubricItem(pts, desc))
    return Rubric(max_points=max_points, expected_answer=str(d.get("expected_answer", "")).strip(), items=items)

def format_rubric(d: dict, max_points: float) -> str:
    lines = [f"Expected answer: {d.get('expected_answer', '').strip()}", "", "Criteria:"]
    total = 0.0
    for c in d.get("criteria", []):
        pts = float(c.get("points", 0))
        total += pts
        lines.append(f"- [{pts:g} pt] {str(c.get('description', '')).strip()}")
    if abs(total - max_points) > 1e-6:
        lines.append(f"(Note: criteria sum to {total:g}, cap the score at {max_points:g}.)")
    return "\n".join(lines)

def image_to_b64(img) -> str:
    import cv2, base64
    if isinstance(img, str):
        with open(img, "rb") as f:
            return base64.b64encode(f.read()).decode()
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("encode failed")
    return base64.b64encode(buf.tobytes()).decode()

MAX_IMAGE_PIXELS = 1_400_000   # ~1400 vision tokens on Qwen-VL; bigger images crashed LM Studio's
                               # Vulkan engine ("failed to process mtmd chunk", then device lost)

def prepare_crop(img, min_side: int = 384, max_side: int = 1600):
    """Size a crop for the vision model: upscale small crops so handwriting is readable, and
    downscale large ones (longest side <= max_side, at most MAX_IMAGE_PIXELS)."""
    import cv2
    h, w = img.shape[:2]
    cap = min(max_side / max(1, max(h, w)), (MAX_IMAGE_PIXELS / max(1, h * w)) ** 0.5)
    s = min(min_side / max(1, min(h, w)), cap)
    if s > 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_CUBIC)
    elif cap < 1:
        img = cv2.resize(img, (max(1, int(w * cap)), max(1, int(h * cap))), interpolation=cv2.INTER_AREA)
    return img
