"""SQLite persistence layer."""
import sqlite3
import json
from typing import List, Optional
from .config import db_path
from .models import Student, TestTemplate, Submission, Grade, region_to_dict, region_from_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    student_id TEXT,
    email TEXT
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    image_path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    regions_json TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER,
    template_id INTEGER,
    scan_front_path TEXT,
    scan_back_path TEXT,
    aligned_front_path TEXT,
    has_work_on_back INTEGER DEFAULT 0,
    back_reviewed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER,
    region_id INTEGER,
    problem TEXT,
    grp TEXT,
    score REAL,
    max_points REAL,
    feedback TEXT,
    needs_human_review INTEGER DEFAULT 0,
    review_reason TEXT,
    extracted_image_path TEXT
);
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER,
    channel TEXT,          -- 'email' | 'lms' | 'canvas'
    recipient TEXT,        -- email address, export folder for lms, 'course/assignment' for canvas
    status TEXT,           -- 'sent' | 'failed' | 'exported' | 'dry_run'
    error TEXT,
    sent_at TEXT,
    pdf_path TEXT
);
CREATE TABLE IF NOT EXISTS exams (
    exam_name TEXT PRIMARY KEY,
    instructions TEXT       -- professor's grading instructions, shared by all versions
);
"""

# Columns added after the initial schema: (table, column, type)
MIGRATIONS = [
    ("templates", "exam_name", "TEXT"),
    ("templates", "version_label", "TEXT DEFAULT 'A'"),
    ("templates", "name_rect_json", "TEXT"),
    ("submissions", "name_read", "TEXT"),
    ("submissions", "name_match_score", "REAL DEFAULT 0"),
    ("grades", "transcription", "TEXT"),
    # multi-page templates
    ("templates", "pages_json", "TEXT"),
    ("templates", "name_page", "INTEGER DEFAULT 0"),
    # persisted grading sessions
    ("submissions", "exam_name", "TEXT"),
    ("submissions", "status", "TEXT DEFAULT 'graded'"),
    ("submissions", "scan_pages_json", "TEXT"),
    ("submissions", "back_pages_json", "TEXT"),
    ("submissions", "aligned_pages_json", "TEXT"),
    ("submissions", "name_confirmed", "INTEGER DEFAULT 1"),
    ("submissions", "error", "TEXT"),
    ("submissions", "source", "TEXT"),
    ("submissions", "created_at", "TEXT"),
    # confidence-based human grading
    ("grades", "confidence", "REAL DEFAULT 1"),
    ("grades", "human_graded", "INTEGER DEFAULT 0"),
    # blank pages in scans (double-sided scanning) and column layout hint
    ("templates", "scan_layout_json", "TEXT"),
    ("templates", "columns", "INTEGER DEFAULT 0"),
    # student ID field (matched exactly against the roster before trying the name)
    ("templates", "id_rect_json", "TEXT"),
    ("templates", "id_page", "INTEGER DEFAULT 0"),
    ("submissions", "id_read", "TEXT"),
    # shared stems of questions with parts ({"1": "..."}), sent as context when grading each part
    ("templates", "stems_json", "TEXT"),
    # note a part's grader leaves for the later parts (an error carried forward is not penalized twice)
    ("grades", "next_part_notes", "TEXT"),
    # version field on the template (read to pick the version) + how/whether a test's version was set
    ("templates", "version_rect_json", "TEXT"),
    ("templates", "version_page", "INTEGER DEFAULT 0"),
    ("submissions", "version_read", "TEXT"),
    ("submissions", "version_confirmed", "INTEGER DEFAULT 0"),
    # grading strictness per exam (5 very generous .. 10 strict); NULL = 10, the original behavior
    ("exams", "strictness", "INTEGER"),
]

_migrated = False

def _migrate(conn):
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # legacy templates: each becomes its own exam with version A
    conn.execute("UPDATE templates SET exam_name=name WHERE exam_name IS NULL OR exam_name=''")
    conn.execute("UPDATE templates SET version_label='A' WHERE version_label IS NULL OR version_label=''")

def connect():
    global _migrated
    p = db_path()
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    if not _migrated:
        conn.execute("PRAGMA journal_mode=WAL")   # crash-safe, readers don't block the grading thread
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _migrated = True
    return conn

# ---- Students ----
def add_student(s: Student) -> int:
    c = connect()
    cur = c.execute("INSERT INTO students(name,student_id,email) VALUES(?,?,?)",
                    (s.name, s.student_id, s.email))
    c.commit(); sid = cur.lastrowid; c.close()
    return sid

def list_students() -> List[Student]:
    c = connect()
    rows = c.execute("SELECT * FROM students ORDER BY name").fetchall()
    c.close()
    return [Student(id=r["id"], name=r["name"], student_id=r["student_id"] or "", email=r["email"] or "") for r in rows]

def delete_student(sid: int):
    c = connect()
    c.execute("DELETE FROM students WHERE id=?", (sid,))
    c.commit(); c.close()

# ---- Templates (exam versions) ----
def save_template(t: TestTemplate) -> int:
    c = connect()
    regions = json.dumps([region_to_dict(r) for r in t.regions])
    name_rect = json.dumps(list(t.name_rect)) if t.name_rect else None
    exam = t.exam_name or t.name
    name = t.name or f"{exam} v{t.version_label}"
    image_path = t.pages[0] if t.pages else t.image_path
    pages = json.dumps(t.pages)
    vals = (name, image_path, t.width, t.height, regions, exam, t.version_label, name_rect, pages, t.name_page,
            json.dumps(t.scan_layout), int(t.columns or 0),
            json.dumps(list(t.id_rect)) if t.id_rect else None, int(t.id_page or 0),
            json.dumps(t.stems or {}),
            json.dumps(list(t.version_rect)) if t.version_rect else None, int(t.version_page or 0))
    if t.id:
        c.execute("""UPDATE templates SET name=?,image_path=?,width=?,height=?,regions_json=?,
                     exam_name=?,version_label=?,name_rect_json=?,pages_json=?,name_page=?,
                     scan_layout_json=?,columns=?,id_rect_json=?,id_page=?,stems_json=?,
                     version_rect_json=?,version_page=? WHERE id=?""",
                  vals + (t.id,))
        tid = t.id
    else:
        cur = c.execute("""INSERT INTO templates(name,image_path,width,height,regions_json,exam_name,version_label,
                                                 name_rect_json,pages_json,name_page,scan_layout_json,columns,
                                                 id_rect_json,id_page,stems_json,version_rect_json,version_page)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
        tid = cur.lastrowid
    c.commit(); c.close()
    return tid

def _template_from_row(row) -> TestTemplate:
    regions = []
    if row["regions_json"]:
        for i, d in enumerate(json.loads(row["regions_json"])):
            d["id"] = i
            regions.append(region_from_dict(d))
    name_rect = json.loads(row["name_rect_json"]) if row["name_rect_json"] else None
    pages = json.loads(row["pages_json"]) if row["pages_json"] else []
    return TestTemplate(id=row["id"], name=row["name"], image_path=row["image_path"],
                        width=row["width"], height=row["height"], regions=regions,
                        exam_name=row["exam_name"] or row["name"], version_label=row["version_label"] or "A",
                        name_rect=tuple(name_rect) if name_rect else None,
                        name_page=row["name_page"] or 0, page_paths=pages,
                        scan_layout=json.loads(row["scan_layout_json"]) if row["scan_layout_json"] else [],
                        columns=row["columns"] or 0,
                        id_rect=tuple(json.loads(row["id_rect_json"])) if row["id_rect_json"] else None,
                        id_page=row["id_page"] or 0,
                        stems=json.loads(row["stems_json"]) if row["stems_json"] else {},
                        version_rect=tuple(json.loads(row["version_rect_json"])) if row["version_rect_json"] else None,
                        version_page=row["version_page"] or 0)

def load_template(tid: int) -> Optional[TestTemplate]:
    c = connect()
    row = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    c.close()
    return _template_from_row(row) if row else None

def list_templates() -> List[TestTemplate]:
    c = connect()
    rows = c.execute("SELECT * FROM templates ORDER BY exam_name, version_label").fetchall()
    c.close()
    return [_template_from_row(r) for r in rows]

def delete_template(tid: int):
    c = connect()
    c.execute("DELETE FROM templates WHERE id=?", (tid,))
    c.commit(); c.close()

def list_exams() -> List[str]:
    c = connect()
    rows = c.execute("SELECT DISTINCT exam_name FROM templates ORDER BY exam_name").fetchall()
    c.close()
    return [r["exam_name"] for r in rows]

def list_versions(exam_name: str) -> List[TestTemplate]:
    c = connect()
    rows = c.execute("SELECT * FROM templates WHERE exam_name=? ORDER BY version_label", (exam_name,)).fetchall()
    c.close()
    return [_template_from_row(r) for r in rows]

# ---- Submissions ----
_SUB_COLS = ("student_id", "template_id", "scan_front_path", "scan_back_path", "aligned_front_path",
             "has_work_on_back", "back_reviewed", "name_read", "name_match_score", "exam_name", "status",
             "scan_pages_json", "back_pages_json", "aligned_pages_json", "name_confirmed", "error", "source",
             "created_at", "id_read", "version_read", "version_confirmed")

def _sub_values(sub: Submission):
    pages, backs = sub.pages, sub.backs
    return (sub.student_id, sub.template_id, pages[0] if pages else "",
            next((b for b in backs if b), ""), sub.aligned_pages[0] if sub.aligned_pages else sub.aligned_front_path,
            int(sub.has_work_on_back), int(sub.back_reviewed), sub.name_read, sub.name_match_score,
            sub.exam_name, sub.status, json.dumps(pages), json.dumps(backs), json.dumps(sub.aligned_pages),
            int(sub.name_confirmed), sub.error, sub.source, sub.created_at, sub.id_read,
            sub.version_read, int(sub.version_confirmed))

def add_submission(sub: Submission) -> int:
    if not sub.created_at:
        from datetime import datetime
        sub.created_at = datetime.now().isoformat(timespec="seconds")
    c = connect()
    cur = c.execute(f"INSERT INTO submissions({','.join(_SUB_COLS)}) VALUES({','.join('?' * len(_SUB_COLS))})",
                    _sub_values(sub))
    c.commit(); sid = cur.lastrowid; c.close()
    sub.id = sid
    return sid

def update_submission(sub: Submission):
    c = connect()
    c.execute(f"UPDATE submissions SET {','.join(f'{k}=?' for k in _SUB_COLS)} WHERE id=?",
              _sub_values(sub) + (sub.id,))
    c.commit(); c.close()

def get_submission(sub_id: int) -> Optional[Submission]:
    c = connect()
    row = c.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    c.close()
    return _sub_from_row(row) if row else None

def list_submissions_by_status(exam_name: str = None, statuses=None) -> List[Submission]:
    """Work queue: submissions of an exam (any version, incl. not yet identified) in the given statuses."""
    c = connect()
    q, args = "SELECT * FROM submissions WHERE 1=1", []
    if exam_name is not None:
        q += " AND exam_name=?"; args.append(exam_name)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"; args.extend(statuses)
    rows = c.execute(q + " ORDER BY id", args).fetchall()
    c.close()
    return [_sub_from_row(r) for r in rows]

def delete_grades(sub_id: int):
    c = connect()
    c.execute("DELETE FROM grades WHERE submission_id=?", (sub_id,))
    c.commit(); c.close()

def _sub_from_row(r) -> Submission:
    def lst(k):
        return json.loads(r[k]) if r[k] else []
    return Submission(id=r["id"], student_id=r["student_id"], template_id=r["template_id"],
                      scan_front_path=r["scan_front_path"] or "", scan_back_path=r["scan_back_path"] or "",
                      aligned_front_path=r["aligned_front_path"] or "",
                      has_work_on_back=bool(r["has_work_on_back"]), back_reviewed=bool(r["back_reviewed"]),
                      name_read=r["name_read"] or "", name_match_score=r["name_match_score"] or 0.0,
                      exam_name=r["exam_name"] or "", status=r["status"] or "graded",
                      scan_pages=lst("scan_pages_json"), back_pages=lst("back_pages_json"),
                      aligned_pages=lst("aligned_pages_json"),
                      name_confirmed=bool(r["name_confirmed"]) if r["name_confirmed"] is not None else True,
                      error=r["error"] or "", source=r["source"] or "", created_at=r["created_at"] or "",
                      id_read=r["id_read"] or "", version_read=r["version_read"] or "",
                      version_confirmed=bool(r["version_confirmed"]))

def delete_submission(sub_id: int):
    c = connect()
    c.execute("DELETE FROM grades WHERE submission_id=?", (sub_id,))
    c.execute("DELETE FROM submissions WHERE id=?", (sub_id,))
    c.commit(); c.close()

def list_submissions(template_id=None, template_ids=None) -> List[Submission]:
    c = connect()
    if template_ids is not None:
        ids = list(template_ids) or [-1]
        q = ",".join("?" * len(ids))
        rows = c.execute(f"SELECT * FROM submissions WHERE template_id IN ({q}) ORDER BY id", ids).fetchall()
    elif template_id:
        rows = c.execute("SELECT * FROM submissions WHERE template_id=? ORDER BY id", (template_id,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM submissions ORDER BY id").fetchall()
    c.close()
    return [_sub_from_row(r) for r in rows]

# ---- Grades ----
def save_grade(g: Grade) -> int:
    c = connect()
    if g.id:
        c.execute("""UPDATE grades SET score=?,feedback=?,needs_human_review=?,review_reason=?,extracted_image_path=?,
                     transcription=?,confidence=?,human_graded=?,next_part_notes=? WHERE id=?""",
                  (g.score, g.feedback, int(g.needs_human_review), g.review_reason, g.extracted_image_path,
                   g.transcription, g.confidence, int(g.human_graded), g.next_part_notes, g.id))
        gid = g.id
    else:
        cur = c.execute("""INSERT INTO grades(submission_id,region_id,problem,grp,score,max_points,feedback,
                                              needs_human_review,review_reason,extracted_image_path,transcription,
                                              confidence,human_graded,next_part_notes)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (g.submission_id, g.region_id, g.problem, g.group, g.score, g.max_points,
                         g.feedback, int(g.needs_human_review), g.review_reason, g.extracted_image_path,
                         g.transcription, g.confidence, int(g.human_graded), g.next_part_notes))
        gid = cur.lastrowid
    c.commit(); c.close()
    return gid

def list_grades(submission_id: int) -> List[Grade]:
    c = connect()
    rows = c.execute("SELECT * FROM grades WHERE submission_id=? ORDER BY id", (submission_id,)).fetchall()
    c.close()
    return [Grade(id=r["id"], submission_id=r["submission_id"], region_id=r["region_id"],
                  problem=r["problem"] or "", group=r["grp"] or "", score=r["score"] or 0,
                  max_points=r["max_points"] or 0, feedback=r["feedback"] or "",
                  needs_human_review=bool(r["needs_human_review"]), review_reason=r["review_reason"] or "",
                  extracted_image_path=r["extracted_image_path"] or "",
                  transcription=r["transcription"] or "",
                  confidence=r["confidence"] if r["confidence"] is not None else 1.0,
                  human_graded=bool(r["human_graded"]),
                  next_part_notes=r["next_part_notes"] or "") for r in rows]

# ---- Exam settings ----
def get_exam_instructions(exam_name: str) -> str:
    c = connect()
    row = c.execute("SELECT instructions FROM exams WHERE exam_name=?", (exam_name,)).fetchone()
    c.close()
    return (row["instructions"] or "") if row else ""

def set_exam_instructions(exam_name: str, instructions: str):
    c = connect()
    c.execute("INSERT INTO exams(exam_name,instructions) VALUES(?,?) "
              "ON CONFLICT(exam_name) DO UPDATE SET instructions=excluded.instructions",
              (exam_name, instructions))
    c.commit(); c.close()

def get_exam_strictness(exam_name: str) -> int:
    """5 (very generous) .. 10 (strict, the default)."""
    from .config import clamp_strictness
    c = connect()
    row = c.execute("SELECT strictness FROM exams WHERE exam_name=?", (exam_name,)).fetchone()
    c.close()
    return clamp_strictness(row["strictness"] if row else None)

def set_exam_strictness(exam_name: str, level: int):
    from .config import clamp_strictness
    c = connect()
    c.execute("INSERT INTO exams(exam_name,strictness) VALUES(?,?) "
              "ON CONFLICT(exam_name) DO UPDATE SET strictness=excluded.strictness",
              (exam_name, clamp_strictness(level)))
    c.commit(); c.close()

# ---- Returns (emails sent / LMS exports) ----
def add_return(submission_id: int, channel: str, recipient: str, status: str, error: str = "",
               pdf_path: str = "") -> int:
    from datetime import datetime
    c = connect()
    cur = c.execute("""INSERT INTO returns(submission_id,channel,recipient,status,error,sent_at,pdf_path)
                       VALUES(?,?,?,?,?,?,?)""",
                    (submission_id, channel, recipient, status, error,
                     datetime.now().isoformat(timespec="seconds"), pdf_path))
    c.commit(); rid = cur.lastrowid; c.close()
    return rid

def list_returns(submission_id: int = None) -> List[dict]:
    c = connect()
    if submission_id is None:
        rows = c.execute("SELECT * FROM returns ORDER BY id").fetchall()
    else:
        rows = c.execute("SELECT * FROM returns WHERE submission_id=? ORDER BY id", (submission_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]

def was_emailed(submission_id: int, recipient: str) -> bool:
    """True if this submission was already successfully emailed to this address."""
    return was_returned(submission_id, "email", recipient)

def was_returned(submission_id: int, channel: str, recipient: str) -> bool:
    """True if this submission was already sent successfully over `channel` to `recipient`
    (an email address, or 'course/assignment' for Canvas)."""
    c = connect()
    row = c.execute("""SELECT 1 FROM returns WHERE submission_id=? AND channel=? AND status='sent'
                       AND lower(recipient)=lower(?) LIMIT 1""", (submission_id, channel, recipient)).fetchone()
    c.close()
    return row is not None

def last_return(submission_id: int, channel: str = None) -> Optional[dict]:
    c = connect()
    q, args = "SELECT * FROM returns WHERE submission_id=?", [submission_id]
    if channel:
        q += " AND channel=?"; args.append(channel)
    row = c.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    c.close()
    return dict(row) if row else None
