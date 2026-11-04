"""Application configuration and constants."""
import os
import json
from pathlib import Path

APP_NAME = "LMGrader"
APP_VERSION = "0.2.0"

# Default LM Studio endpoint (OpenAI-compatible)
DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LLM_API_KEY = "lm-studio"
DEFAULT_LLM_MODEL = "local-model"

# Mutable runtime config (persisted to settings.json)
LLM_CONFIG = {
    "base_url": DEFAULT_LLM_BASE_URL,
    "api_key": DEFAULT_LLM_API_KEY,
    "model": DEFAULT_LLM_MODEL,
    "max_tokens": 2048,
    "temperature": 0.1,
    # grades below this model confidence go to a human (0 disables)
    "review_confidence": 0.7,
    # full marks from the AI: don't send to a human for low confidence unless it is below this floor
    "full_marks_skip_review": False,
    "full_marks_min_confidence": 0.3,
    # full marks from the AI: writing on the back page doesn't send the question to a human
    "full_marks_ignore_back": True,
    # grading instructions pre-filled for new exams (each exam can override)
    "default_instructions": "",
    # grading strictness pre-set for new exams: 5 (very generous) .. 10 (strict)
    "default_strictness": 10,
}

STRICTNESS_MIN, STRICTNESS_MAX = 5, 10

def clamp_strictness(level) -> int:
    """A stored/entered level as 5..10; missing or invalid -> 10 (strict, the original behavior)."""
    try:
        return max(STRICTNESS_MIN, min(STRICTNESS_MAX, int(level)))
    except (TypeError, ValueError):
        return STRICTNESS_MAX

# Appearance: "light" | "dark" | "system"
UI_CONFIG = {"theme": "light"}

# Phrases (regex, case-insensitive) in a transcription meaning "the work continues elsewhere".
# Any match sends the question to a human, regardless of what the model says.
CONTINUATION_PATTERNS = [
    r"\b(see|look at|check|turn|cont(inued|inues|\.)?( on)?)\b(?!\s+fractions?).{0,15}\b(back|reverse|over|next page|other side|overleaf|behind)\b",
    r"\b(on|at) the back\b", r"\bp\.\s*t\.\s*o\b", r"\bcontinued\b(?!\s+fractions?)", r"\bcont'd\b",
    r"\b(ver|sigue|contin[uú]a|contin[uú]o)\b.{0,15}\b(atr[aá]s|detr[aá]s|reverso|vuelta|dorso|siguiente)\b",
    r"\bal (reverso|dorso)\b", r"\bv[eé]ase (atr[aá]s|reverso|dorso)\b",
    r"[→⟶➜]\s*(back|atr[aá]s|reverso)",
]

# Email (SMTP) for returning graded tests. The password is NEVER stored here: it lives in the
# OS keyring (if the `keyring` package is installed) or only in memory for the session.
SMTP_CONFIG = {
    "host": "smtp.gmail.com",
    "port": 465,
    "security": "SSL",          # SSL | STARTTLS | none
    "username": "",
    "from_name": "",
    "from_address": "",
    "delay": 2.0,               # seconds between messages
    "subject": "{exam}: your graded test ({total}/{max})",
    "body": ("Hi {first_name},\n\n"
             "Attached is your graded {exam} (version {version}) with feedback.\n"
             "Score: {total} / {max} ({percent}%).\n\n"
             "Let me know if you have any questions."),
}

# Canvas (feedback PDFs as submission comments). The access token is NEVER stored here (keyring or
# session memory, like the SMTP password). targets: {exam name: {"course_id", "assignment_id"}}.
CANVAS_CONFIG = {
    "base_url": "",             # e.g. https://canvas.charlotte.edu
    "comment": "Your graded {exam} with feedback is attached.",
    "targets": {},
}

# Max points of every new question (detected or drawn); the professor changes it per question
DEFAULT_POINTS = 1.0

# Image processing
TEMPLATE_MATCH_MIN_MATCHES = 10
MIN_ALIGN_INLIERS = 25       # below this, alignment is considered unreliable
CROP_PADDING = 8             # px added around each answer crop
ALIGNMENT_SCALE = 1.0
INK_THRESHOLD = 128  # grayscale below this counts as ink
BACK_PAGE_INK_RATIO = 0.002  # (legacy) fraction of pixels that must be ink to flag "work on back"

# Back-page analysis. Pages are normalised to BACK_REF_WIDTH px wide (~100 dpi for Letter) so
# the pixel thresholds below are resolution independent.
BACK_REF_WIDTH = 850
BACK_MARGIN = 0.04           # ignore this fraction on every side (scanner edge lines, holes)
BACK_INK_GRAY = 165          # gray below this is ink (pencil included)
BACK_MIN_BLOB = 22           # a blob must be at least this wide or tall to count (drops dots/specks)
BACK_MIN_BLOB_INK = 25       # ...and contain at least this many ink pixels
BACK_MINOR_INK = 40          # total ink of kept blobs >= this -> 'minor' (a name, a scribble)
BACK_WORK_INK = 900          # total ink of kept blobs >= this -> 'work' (needs grading/review)

# Name matching (rapidfuzz WRatio, 0-100)
NAME_ACCEPT_SCORE = 88       # auto-accept the top match at or above this score...
NAME_ACCEPT_MARGIN = 8       # ...if it beats the runner-up by at least this much
NAME_CONFIRM_SCORE = 60      # LLM-picked matches below this are flagged for confirmation

# Paths
def data_dir() -> Path:
    base = Path(os.environ.get("LMGRADER_DATA", Path.home() / ".lmgrader"))
    base.mkdir(parents=True, exist_ok=True)
    return base

def db_path() -> Path:
    return data_dir() / "lmgrader.db"

def settings_path() -> Path:
    return data_dir() / "settings.json"

def load_settings():
    p = settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        if "llm" in data or "smtp" in data:
            LLM_CONFIG.update(data.get("llm", {}))
            SMTP_CONFIG.update({k: v for k, v in data.get("smtp", {}).items() if k != "password"})
            UI_CONFIG.update(data.get("ui", {}))
            CANVAS_CONFIG.update({k: v for k, v in data.get("canvas", {}).items() if k != "token"})
        else:  # legacy flat file (LLM settings only)
            LLM_CONFIG.update(data)

def save_settings():
    smtp = {k: v for k, v in SMTP_CONFIG.items() if k != "password"}
    canvas = {k: v for k, v in CANVAS_CONFIG.items() if k != "token"}
    settings_path().write_text(json.dumps({"llm": LLM_CONFIG, "smtp": smtp, "ui": UI_CONFIG, "canvas": canvas},
                                          indent=2), encoding="utf-8")

load_settings()
