"""SQLite persistence layer."""
import sqlite3
import json
from typing import List, Optional
from .config import db_path
from .models import Student, TestTemplate, Submission, Grade, Rubric, region_to_dict, region_from_dict, new_uid

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
CREATE TABLE IF NOT EXISTS exams (   -- before courses (kept; copied to course_exams once)
    exam_name TEXT PRIMARY KEY,
    instructions TEXT       -- professor's grading instructions, shared by all versions
);
-- courses: each has its own roster (students.course_id) and exams (templates/submissions.course_id);
-- an exam is identified by (course, exam name)
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS course_exams (
    course_id INTEGER,
    exam_name TEXT,
    instructions TEXT,      -- professor's grading instructions, shared by all versions
    strictness INTEGER,     -- 5 very generous .. 10 strict; NULL = 10
    PRIMARY KEY (course_id, exam_name)
);
-- hand grading, per question (template version + region uid)
CREATE TABLE IF NOT EXISTS hand_questions (
    template_id INTEGER,
    region_uid TEXT,
    stage TEXT DEFAULT 'start',   -- start | blanks | group | grade
    -- (migrations) kind: blank | short | choice (how answers are grouped automatically);
    -- choices_json: multiple choice boxes [{"label", "rect"}] in template page pixels
    PRIMARY KEY (template_id, region_uid)
);
CREATE TABLE IF NOT EXISTS hand_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER,
    region_uid TEXT,
    label TEXT,
    is_blank INTEGER DEFAULT 0,   -- the answers sorted out as blank
    items_json TEXT,              -- rubric item ids applied to every member
    comment TEXT,
    created_at TEXT
    -- (migrations) auto_key: what LMGrader grouped by ("blank", "choice:B", "text:x=2"), NULL for
    -- groups made by hand; checked: 0 = suggested by LMGrader and not checked by the human yet
);
CREATE TABLE IF NOT EXISTS hand_group_members (
    group_id INTEGER,
    submission_id INTEGER,
    PRIMARY KEY (group_id, submission_id)
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
    # hand grading with structured rubrics
    ("grades", "source", "TEXT DEFAULT ''"),
    ("grades", "rubric_items_json", "TEXT"),
    ("grades", "comment", "TEXT"),
    ("grades", "hand_group_id", "INTEGER"),
    # hand grading categories (blank / short answer / multiple choice) and suggested groups
    ("hand_questions", "kind", "TEXT"),
    ("hand_questions", "choices_json", "TEXT"),
    ("hand_groups", "auto_key", "TEXT"),
    ("hand_groups", "checked", "INTEGER DEFAULT 1"),
    # courses
    ("students", "course_id", "INTEGER"),
    ("templates", "course_id", "INTEGER"),
    ("submissions", "course_id", "INTEGER"),
]
DEFAULT_COURSE = "My course"

_migrated = False
RUBRIC_BACKUP = "lmgrader-backup-before-structured-rubrics.db"

def _legacy_regions(conn) -> list:
    """Templates saved before structured rubrics: [(template id, converted regions_json)].
    Text rubrics become items (with ids that hand grades can refer to) and every region gets its
    uid (its index, as _template_from_row assumes), so saved grades keep their question."""
    from .models import Rubric
    out = []
    for row in conn.execute("SELECT id, regions_json FROM templates").fetchall():
        try:
            regions = json.loads(row["regions_json"] or "[]")
        except ValueError:
            continue
        changed = False
        for i, d in enumerate(regions):
            if not isinstance(d, dict):
                continue
            if not d.get("uid"):
                d["uid"], changed = _legacy_uid(i), True
            if not isinstance(d.get("rubric"), dict):
                pts = float(d.get("points") or 0)
                d["rubric"], changed = Rubric.from_obj(d.get("rubric") or "", pts).to_dict(pts), True
        if changed:
            out.append((row["id"], json.dumps(regions)))
    return out

def _backup(conn, name: str):
    """Copy of the whole database next to it (kept: an existing backup is never overwritten)."""
    dest = db_path().with_name(name)
    if dest.exists():
        return
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()

def _migrate(conn):
    legacy = _legacy_regions(conn)
    if legacy:
        _backup(conn, RUBRIC_BACKUP)
    for tid, regions_json in legacy:
        conn.execute("UPDATE templates SET regions_json=? WHERE id=?", (regions_json, tid))
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # legacy templates: each becomes its own exam with version A
    conn.execute("UPDATE templates SET exam_name=name WHERE exam_name IS NULL OR exam_name=''")
    conn.execute("UPDATE templates SET version_label='A' WHERE version_label IS NULL OR version_label=''")
    _assign_courses(conn)

def _assign_courses(conn):
    """Data saved before courses (roster, exams, scans, exam settings) goes into one course,
    which the user renames; nothing is deleted."""
    from datetime import datetime
    first = conn.execute("SELECT id FROM courses ORDER BY id LIMIT 1").fetchone()
    if first is None:
        cid = conn.execute("INSERT INTO courses(name, created_at) VALUES(?,?)",
                           (DEFAULT_COURSE, datetime.now().isoformat(timespec="seconds"))).lastrowid
        conn.execute("INSERT OR IGNORE INTO course_exams(course_id, exam_name, instructions, strictness) "
                     "SELECT ?, exam_name, instructions, strictness FROM exams", (cid,))
    else:
        cid = first["id"]
    for table in ("students", "templates", "submissions"):
        conn.execute(f"UPDATE {table} SET course_id=? WHERE course_id IS NULL", (cid,))

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

# ---- Courses ----
# Every tab works in one course (the sidebar picker): rosters, exams and scans are listed for the
# current course unless a course is given. Jobs that run in the background use the course of the
# template / submission they work on; the course can't be switched while a job runs.
_current_course: Optional[int] = None

def current_course() -> int:
    """The course the tabs show (remembered in settings as UI_CONFIG["course_id"])."""
    global _current_course
    if _current_course is None:
        from .config import UI_CONFIG
        ids = [c["id"] for c in list_courses()]
        want = UI_CONFIG.get("course_id")
        _current_course = want if want in ids else ids[0]      # there is always a course
    return _current_course

def set_current_course(course_id: int):
    global _current_course
    _current_course = course_id

def _course(course_id: Optional[int]) -> int:
    return course_id if course_id is not None else current_course()

def list_courses() -> List[dict]:
    """[{id, name, students, exams, tests}] by name."""
    c = connect()
    rows = c.execute("""SELECT c.id, c.name,
                          (SELECT COUNT(*) FROM students s WHERE s.course_id=c.id) AS students,
                          (SELECT COUNT(DISTINCT exam_name) FROM templates t WHERE t.course_id=c.id) AS exams,
                          (SELECT COUNT(*) FROM submissions u WHERE u.course_id=c.id) AS tests
                        FROM courses c ORDER BY c.name COLLATE NOCASE, c.id""").fetchall()
    c.close()
    return [dict(r) for r in rows]

def course_name(course_id: int) -> str:
    c = connect()
    row = c.execute("SELECT name FROM courses WHERE id=?", (course_id,)).fetchone()
    c.close()
    return row["name"] if row else ""

def _check_course_name(c, name: str, course_id: int = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("A course needs a name.")
    if c.execute("SELECT 1 FROM courses WHERE name=? COLLATE NOCASE AND id IS NOT ?", (name, course_id)).fetchone():
        raise ValueError(f"There is already a course named “{name}”.")
    return name

def add_course(name: str) -> int:
    from datetime import datetime
    c = connect()
    try:
        name = _check_course_name(c, name)
        cid = c.execute("INSERT INTO courses(name, created_at) VALUES(?,?)",
                        (name, datetime.now().isoformat(timespec="seconds"))).lastrowid
        c.commit()
    finally:
        c.close()
    return cid

def rename_course(course_id: int, name: str):
    c = connect()
    try:
        c.execute("UPDATE courses SET name=? WHERE id=?", (_check_course_name(c, name, course_id), course_id))
        c.commit()
    finally:
        c.close()

def delete_course(course_id: int):
    """Only a course without students or exams can be deleted (and never the last one). Scanned
    tests left from exams deleted before (no tab shows them) are deleted with it."""
    info = next((x for x in list_courses() if x["id"] == course_id), None)
    if info is None:
        return
    if info["students"] or info["exams"]:
        raise ValueError(f"“{info['name']}” still has {info['students']} students and {info['exams']} exams: "
                         f"move or delete them first.")
    if len(list_courses()) <= 1:
        raise ValueError("LMGrader needs at least one course.")
    c = connect()
    _delete_submissions(c, [r["id"] for r in c.execute("SELECT id FROM submissions WHERE course_id=?",
                                                       (course_id,))])
    c.execute("DELETE FROM course_exams WHERE course_id=?", (course_id,))
    c.execute("DELETE FROM courses WHERE id=?", (course_id,))
    c.commit(); c.close()
    if _current_course == course_id:
        set_current_course(None)

# ---- Students ----
def add_student(s: Student) -> int:
    s.course_id = _course(s.course_id)
    c = connect()
    cur = c.execute("INSERT INTO students(name,student_id,email,course_id) VALUES(?,?,?,?)",
                    (s.name, s.student_id, s.email, s.course_id))
    c.commit(); sid = cur.lastrowid; c.close()
    s.id = sid
    return sid

def list_students(course_id: int = None, all_courses: bool = False) -> List[Student]:
    """The roster of a course (default: the current one); all_courses for id lookups."""
    c = connect()
    if all_courses:
        rows = c.execute("SELECT * FROM students ORDER BY name").fetchall()
    else:
        rows = c.execute("SELECT * FROM students WHERE course_id=? ORDER BY name", (_course(course_id),)).fetchall()
    c.close()
    return [Student(id=r["id"], name=r["name"], student_id=r["student_id"] or "", email=r["email"] or "",
                    course_id=r["course_id"]) for r in rows]

def move_students(student_ids: List[int], course_id: int):
    """Students change roster; their scanned tests keep pointing at them."""
    if not student_ids:
        return
    c = connect()
    c.executemany("UPDATE students SET course_id=? WHERE id=?", [(course_id, s) for s in student_ids])
    c.commit(); c.close()

def delete_student(sid: int):
    c = connect()
    c.execute("DELETE FROM students WHERE id=?", (sid,))
    c.commit(); c.close()

# ---- Templates (exam versions) ----
def save_template(t: TestTemplate) -> int:
    seen = set()
    for r in t.regions:              # uids must be unique within a version
        if not r.uid or r.uid in seen:
            r.uid = new_uid()
        seen.add(r.uid)
    t.course_id = _course(t.course_id)
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
            json.dumps(list(t.version_rect)) if t.version_rect else None, int(t.version_page or 0),
            t.course_id)
    if t.id:
        row = c.execute("SELECT regions_json FROM templates WHERE id=?", (t.id,)).fetchone()
        if row is not None:
            _follow_regions(c, t.id, _region_uids(row["regions_json"]), [r.uid for r in t.regions])
            _rescore_hand_grades(c, t.id, row["regions_json"], t.regions)
        c.execute("""UPDATE templates SET name=?,image_path=?,width=?,height=?,regions_json=?,
                     exam_name=?,version_label=?,name_rect_json=?,pages_json=?,name_page=?,
                     scan_layout_json=?,columns=?,id_rect_json=?,id_page=?,stems_json=?,
                     version_rect_json=?,version_page=?,course_id=? WHERE id=?""",
                  vals + (t.id,))
        tid = t.id
    else:
        cur = c.execute("""INSERT INTO templates(name,image_path,width,height,regions_json,exam_name,version_label,
                                                 name_rect_json,pages_json,name_page,scan_layout_json,columns,
                                                 id_rect_json,id_page,stems_json,version_rect_json,version_page,
                                                 course_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
        tid = cur.lastrowid
    c.commit(); c.close()
    return tid

def _legacy_uid(i: int) -> str:
    """uid of a region saved before regions had one: its index (the same on load and on save)."""
    return f"r{i}"

def _region_uids(regions_json) -> List[str]:
    return [str(d.get("uid") or "") or _legacy_uid(i) for i, d in enumerate(json.loads(regions_json or "[]"))]

def _follow_regions(c, template_id: int, old: List[str], new: List[str]):
    """Regions were reordered/deleted: grades (region_id = index) follow their region, and
    grades of deleted regions are deleted. Runs in the caller's transaction."""
    if old == new:
        return
    pos = {u: j for j, u in enumerate(new)}
    moved = {i: pos[u] for i, u in enumerate(old) if u in pos and pos[u] != i}
    gone = [i for i, u in enumerate(old) if u not in pos]
    subs = "SELECT id FROM submissions WHERE template_id=?"
    if gone:
        c.execute(f"DELETE FROM grades WHERE submission_id IN ({subs}) "
                  f"AND region_id IN ({','.join('?' * len(gone))})", (template_id, *gone))
    if moved:   # one statement: every row is mapped from its old index, so no collisions
        case = " ".join(f"WHEN {i} THEN {j}" for i, j in moved.items())
        c.execute(f"UPDATE grades SET region_id = CASE region_id {case} END "
                  f"WHERE submission_id IN ({subs}) AND region_id IN ({','.join(map(str, moved))})",
                  (template_id,))
    removed = [u for u in old if u not in pos]
    if removed:
        q = ",".join("?" * len(removed))
        c.execute(f"DELETE FROM hand_group_members WHERE group_id IN (SELECT id FROM hand_groups "
                  f"WHERE template_id=? AND region_uid IN ({q}))", (template_id, *removed))
        c.execute(f"DELETE FROM hand_groups WHERE template_id=? AND region_uid IN ({q})", (template_id, *removed))
        c.execute(f"DELETE FROM hand_questions WHERE template_id=? AND region_uid IN ({q})", (template_id, *removed))

def _rescore_hand_grades(c, template_id: int, old_regions_json: str, new_regions: list):
    """Recalculate hand grades for changed rubrics, except grades overridden in Review."""
    old_regions = json.loads(old_regions_json or "[]")
    old_by_uid = {uid: region for uid, region in zip(_region_uids(old_regions_json), old_regions)}
    for new_index, region in enumerate(new_regions):
        old_region = old_by_uid.get(region.uid)
        if old_region is None:
            continue
        old_points = old_region.get("points", 0.0) or 0.0
        old_rubric = Rubric.from_obj(old_region.get("rubric"), old_points)
        if old_points == region.points and old_rubric.to_dict(old_points) == region.rubric.to_dict(region.points):
            continue
        old_item_ids = {item.id for item in old_rubric.items}
        new_item_ids = {item.id for item in region.rubric.items}
        if old_item_ids and not old_item_ids.intersection(new_item_ids):
            continue
        rows = c.execute(
            "SELECT id, score, feedback, rubric_items_json, comment FROM grades "
            "WHERE region_id=? AND source='hand' AND submission_id IN "
            "(SELECT id FROM submissions WHERE template_id=?)",
            (new_index, template_id)).fetchall()
        for grade in rows:
            items = json.loads(grade["rubric_items_json"] or "[]")
            comment = grade["comment"] or ""
            old_score = old_rubric.score(items, old_points)
            old_feedback = old_rubric.feedback(items, comment)
            if abs((grade["score"] or 0.0) - old_score) > 1e-6 or (grade["feedback"] or "") != old_feedback:
                continue
            c.execute("UPDATE grades SET score=?, feedback=?, max_points=? WHERE id=?",
                      (region.rubric.score(items, region.points),
                       region.rubric.feedback(items, comment), region.points, grade["id"]))

def count_template_grades(template_id: int) -> int:
    """Graded answers (all questions) of the tests of a version."""
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM grades WHERE submission_id IN "
                  "(SELECT id FROM submissions WHERE template_id=?)", (template_id,)).fetchone()[0]
    c.close()
    return n

def count_region_grades(template_id: int, region_index: int) -> int:
    """Graded answers of one question (all submissions of the version)."""
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM grades WHERE region_id=? AND submission_id IN "
                  "(SELECT id FROM submissions WHERE template_id=?)", (region_index, template_id)).fetchone()[0]
    c.close()
    return n

def _template_from_row(row) -> TestTemplate:
    regions = []
    if row["regions_json"]:
        for i, d in enumerate(json.loads(row["regions_json"])):
            d["id"] = i
            if not d.get("uid"):
                d["uid"] = _legacy_uid(i)
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
                        version_page=row["version_page"] or 0, course_id=row["course_id"])

def load_template(tid: int) -> Optional[TestTemplate]:
    c = connect()
    row = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    c.close()
    return _template_from_row(row) if row else None

def list_templates(course_id: int = None, all_courses: bool = False) -> List[TestTemplate]:
    """Exam versions of a course (default: the current one)."""
    c = connect()
    if all_courses:
        rows = c.execute("SELECT * FROM templates ORDER BY exam_name, version_label").fetchall()
    else:
        rows = c.execute("SELECT * FROM templates WHERE course_id=? ORDER BY exam_name, version_label",
                         (_course(course_id),)).fetchall()
    c.close()
    return [_template_from_row(r) for r in rows]

def delete_template(tid: int):
    """Delete a version with its hand grading groups. When it is the exam's last version, the
    exam's scanned tests and their grades are deleted too (no tab could show them any more)."""
    c = connect()
    row = c.execute("SELECT course_id, exam_name FROM templates WHERE id=?", (tid,)).fetchone()
    c.execute("DELETE FROM templates WHERE id=?", (tid,))
    c.execute("DELETE FROM hand_group_members WHERE group_id IN (SELECT id FROM hand_groups WHERE template_id=?)",
              (tid,))
    c.execute("DELETE FROM hand_groups WHERE template_id=?", (tid,))
    c.execute("DELETE FROM hand_questions WHERE template_id=?", (tid,))
    if row is not None and not c.execute("SELECT 1 FROM templates WHERE course_id IS ? AND exam_name=?",
                                         (row["course_id"], row["exam_name"])).fetchone():
        _delete_submissions(c, [r["id"] for r in c.execute(
            "SELECT id FROM submissions WHERE course_id IS ? AND exam_name=?", (row["course_id"], row["exam_name"]))])
    c.commit(); c.close()

def exam_test_count(exam_name: str, course_id: int = None) -> int:
    """Scanned tests of an exam (every version, read or not)."""
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM submissions WHERE exam_name=? AND course_id=?",
                  (exam_name, _course(course_id))).fetchone()[0]
    c.close()
    return n

def list_exams(course_id: int = None) -> List[str]:
    """Exam names of a course (default: the current one)."""
    c = connect()
    rows = c.execute("SELECT DISTINCT exam_name FROM templates WHERE course_id=? ORDER BY exam_name",
                     (_course(course_id),)).fetchall()
    c.close()
    return [r["exam_name"] for r in rows]

def list_versions(exam_name: str, course_id: int = None) -> List[TestTemplate]:
    """Versions of the exam `exam_name` of a course (default: the current one)."""
    c = connect()
    rows = c.execute("SELECT * FROM templates WHERE exam_name=? AND course_id=? ORDER BY version_label",
                     (exam_name, _course(course_id))).fetchall()
    c.close()
    return [_template_from_row(r) for r in rows]

def move_exam(exam_name: str, from_course: int, to_course: int):
    """Move an exam (every version, its scanned tests and grades, its settings) to another course."""
    if from_course == to_course:
        return
    if exam_name in list_exams(to_course):
        raise ValueError(f"“{course_name(to_course)}” already has an exam named “{exam_name}”.")
    c = connect()
    c.execute("UPDATE templates SET course_id=? WHERE course_id=? AND exam_name=?", (to_course, from_course, exam_name))
    c.execute("UPDATE submissions SET course_id=? WHERE course_id=? AND exam_name=?",
              (to_course, from_course, exam_name))
    c.execute("INSERT OR REPLACE INTO course_exams(course_id, exam_name, instructions, strictness) "
              "SELECT ?, exam_name, instructions, strictness FROM course_exams WHERE course_id=? AND exam_name=?",
              (to_course, from_course, exam_name))
    c.execute("DELETE FROM course_exams WHERE course_id=? AND exam_name=?", (from_course, exam_name))
    c.commit(); c.close()

def copy_exam(exam_name: str, from_course: int, to_course: int, new_name: str = None) -> List[int]:
    """Copy an exam's versions (pages, boxes, questions, rubrics, multiple choice boxes) and its
    settings to a course, without scans or grades. Returns the new version ids."""
    new_name = (new_name or exam_name).strip()
    if not new_name:
        raise ValueError("The exam needs a name.")
    if new_name in list_exams(to_course):
        raise ValueError(f"“{course_name(to_course)}” already has an exam named “{new_name}”.")
    ids = []
    for t in list_versions(exam_name, from_course):
        old = t.id
        t.id, t.course_id, t.exam_name = None, to_course, new_name
        t.name = f"{new_name} v{t.version_label}"
        new = save_template(t)
        ids.append(new)
        c = connect()
        c.execute("INSERT INTO hand_questions(template_id, region_uid, kind, choices_json) "
                  "SELECT ?, region_uid, kind, choices_json FROM hand_questions WHERE template_id=? "
                  "AND (kind IS NOT NULL OR choices_json IS NOT NULL)", (new, old))
        c.commit(); c.close()
    c = connect()
    c.execute("INSERT OR REPLACE INTO course_exams(course_id, exam_name, instructions, strictness) "
              "SELECT ?, ?, instructions, strictness FROM course_exams WHERE course_id=? AND exam_name=?",
              (to_course, new_name, from_course, exam_name))
    c.commit(); c.close()
    return ids

# ---- Submissions ----
_SUB_COLS = ("student_id", "template_id", "scan_front_path", "scan_back_path", "aligned_front_path",
             "has_work_on_back", "back_reviewed", "name_read", "name_match_score", "exam_name", "status",
             "scan_pages_json", "back_pages_json", "aligned_pages_json", "name_confirmed", "error", "source",
             "created_at", "id_read", "version_read", "version_confirmed", "course_id")

def _sub_values(sub: Submission):
    sub.course_id = _course(sub.course_id)
    pages, backs = sub.pages, sub.backs
    return (sub.student_id, sub.template_id, pages[0] if pages else "",
            next((b for b in backs if b), ""), sub.aligned_pages[0] if sub.aligned_pages else sub.aligned_front_path,
            int(sub.has_work_on_back), int(sub.back_reviewed), sub.name_read, sub.name_match_score,
            sub.exam_name, sub.status, json.dumps(pages), json.dumps(backs), json.dumps(sub.aligned_pages),
            int(sub.name_confirmed), sub.error, sub.source, sub.created_at, sub.id_read,
            sub.version_read, int(sub.version_confirmed), sub.course_id)

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

def list_submissions_by_status(exam_name: str = None, statuses=None, course_id: int = None,
                               all_courses: bool = False) -> List[Submission]:
    """Work queue: submissions of an exam (any version, incl. not yet identified) in the given
    statuses, in a course (default: the current one) or all_courses."""
    c = connect()
    q, args = "SELECT * FROM submissions WHERE 1=1", []
    if not all_courses:
        q += " AND course_id=?"; args.append(_course(course_id))
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
                      version_confirmed=bool(r["version_confirmed"]), course_id=r["course_id"])

def _delete_submissions(c, ids: List[int]):
    """Delete tests with their grades and hand group memberships; groups left empty go too (never
    a question's Blank group). The caller commits."""
    for i in range(0, len(ids), 500):
        chunk = list(ids[i:i + 500])
        q = ",".join("?" * len(chunk))
        c.execute(f"DELETE FROM grades WHERE submission_id IN ({q})", chunk)
        c.execute(f"DELETE FROM hand_group_members WHERE submission_id IN ({q})", chunk)
        c.execute(f"DELETE FROM submissions WHERE id IN ({q})", chunk)
    c.execute("DELETE FROM hand_groups WHERE is_blank=0 AND id NOT IN "
              "(SELECT DISTINCT group_id FROM hand_group_members)")

def delete_submission(sub_id: int):
    c = connect()
    _delete_submissions(c, [sub_id])
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

def set_aligned_page(sub_id: int, page: int, path: str, page_count: int):
    """Store one aligned page without rewriting the rest of the submission (another tab may be
    grading it at the same time)."""
    c = connect()
    row = c.execute("SELECT aligned_pages_json FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if row is not None:
        pages = json.loads(row["aligned_pages_json"] or "[]")
        pages += [""] * (max(page_count, page + 1) - len(pages))
        pages[page] = path
        c.execute("UPDATE submissions SET aligned_pages_json=?, aligned_front_path=? WHERE id=?",
                  (json.dumps(pages), pages[0], sub_id))
        c.commit()
    c.close()

def set_submission_status(sub_id: int, status: str):
    c = connect()
    c.execute("UPDATE submissions SET status=? WHERE id=?", (status, sub_id))
    c.commit(); c.close()

# ---- Grades ----
_GRADE_COLS = ("submission_id", "region_id", "problem", "grp", "score", "max_points", "feedback",
               "needs_human_review", "review_reason", "extracted_image_path", "transcription", "confidence",
               "human_graded", "next_part_notes", "source", "rubric_items_json", "comment", "hand_group_id")

def _grade_values(g: Grade):
    return (g.submission_id, g.region_id, g.problem, g.group, g.score, g.max_points, g.feedback,
            int(g.needs_human_review), g.review_reason, g.extracted_image_path, g.transcription, g.confidence,
            int(g.human_graded), g.next_part_notes, g.source or "",
            json.dumps(list(g.rubric_items)) if g.rubric_items else None, g.comment or "", g.hand_group_id)

def save_grade(g: Grade) -> int:
    c = connect()
    vals = _grade_values(g)
    if g.id:
        cols = _GRADE_COLS[2:]      # a grade never moves to another submission/region
        c.execute(f"UPDATE grades SET {','.join(f'{k}=?' for k in cols)} WHERE id=?", vals[2:] + (g.id,))
        gid = g.id
    else:
        cur = c.execute(f"INSERT INTO grades({','.join(_GRADE_COLS)}) VALUES({','.join('?' * len(_GRADE_COLS))})",
                        vals)
        gid = cur.lastrowid
        g.id = gid
    c.commit(); c.close()
    return gid

def _grade_from_row(r) -> Grade:
    keys = r.keys()
    items = r["rubric_items_json"] if "rubric_items_json" in keys else None
    return Grade(id=r["id"], submission_id=r["submission_id"], region_id=r["region_id"],
                 problem=r["problem"] or "", group=r["grp"] or "", score=r["score"] or 0,
                 max_points=r["max_points"] or 0, feedback=r["feedback"] or "",
                 needs_human_review=bool(r["needs_human_review"]), review_reason=r["review_reason"] or "",
                 extracted_image_path=r["extracted_image_path"] or "",
                 transcription=r["transcription"] or "",
                 confidence=r["confidence"] if r["confidence"] is not None else 1.0,
                 human_graded=bool(r["human_graded"]),
                 next_part_notes=r["next_part_notes"] or "",
                 source=r["source"] or "", rubric_items=json.loads(items) if items else [],
                 comment=r["comment"] or "", hand_group_id=r["hand_group_id"])

def list_grades(submission_id: int) -> List[Grade]:
    c = connect()
    rows = c.execute("SELECT * FROM grades WHERE submission_id=? ORDER BY id", (submission_id,)).fetchall()
    c.close()
    return [_grade_from_row(r) for r in rows]

def get_grade(submission_id: int, region_id: int) -> Optional[Grade]:
    """The saved grade of one question of one test (None if not graded yet)."""
    c = connect()
    row = c.execute("SELECT * FROM grades WHERE submission_id=? AND region_id=? ORDER BY id LIMIT 1",
                    (submission_id, region_id)).fetchone()
    c.close()
    return _grade_from_row(row) if row else None

def human_grade_exists(submission_id: int, region_id: int) -> bool:
    c = connect()
    row = c.execute("SELECT 1 FROM grades WHERE submission_id=? AND region_id=? AND human_graded=1 LIMIT 1",
                    (submission_id, region_id)).fetchone()
    c.close()
    return row is not None

def delete_grade(grade_id: int):
    c = connect()
    c.execute("DELETE FROM grades WHERE id=?", (grade_id,))
    c.commit(); c.close()

def region_grades(template_id: int, region_id: int) -> dict:
    """{submission id: Grade} of one question over every test of a version."""
    c = connect()
    rows = c.execute("SELECT * FROM grades WHERE region_id=? AND submission_id IN "
                     "(SELECT id FROM submissions WHERE template_id=?) ORDER BY id",
                     (region_id, template_id)).fetchall()
    c.close()
    out = {}
    for r in rows:
        out.setdefault(r["submission_id"], _grade_from_row(r))
    return out

# ---- Hand grading (per question: template version + region uid) ----
def get_hand_stage(template_id: int, region_uid: str) -> str:
    c = connect()
    row = c.execute("SELECT stage FROM hand_questions WHERE template_id=? AND region_uid=?",
                    (template_id, region_uid)).fetchone()
    c.close()
    return row["stage"] if row else "start"

def set_hand_stage(template_id: int, region_uid: str, stage: str):
    c = connect()
    c.execute("INSERT INTO hand_questions(template_id,region_uid,stage) VALUES(?,?,?) "
              "ON CONFLICT(template_id,region_uid) DO UPDATE SET stage=excluded.stage",
              (template_id, region_uid, stage))
    c.commit(); c.close()

def get_hand_setup(template_id: int, region_uid: str) -> tuple:
    """(kind, choices) of a question: kind '' (not chosen yet) | 'blank' | 'short' | 'choice';
    choices [{"label", "rect": (x, y, w, h)}] in template page pixels (multiple choice)."""
    c = connect()
    row = c.execute("SELECT kind, choices_json FROM hand_questions WHERE template_id=? AND region_uid=?",
                    (template_id, region_uid)).fetchone()
    c.close()
    if row is None:
        return "", []
    try:
        choices = [{"label": str(d["label"]), "rect": tuple(int(v) for v in d["rect"])}
                   for d in json.loads(row["choices_json"] or "[]")]
    except (ValueError, KeyError, TypeError):
        choices = []
    return row["kind"] or "", choices

def set_hand_setup(template_id: int, region_uid: str, kind: str = None, choices: List[dict] = None):
    """Save the question's grouping category and/or its multiple choice boxes."""
    c = connect()
    c.execute("INSERT OR IGNORE INTO hand_questions(template_id,region_uid) VALUES(?,?)", (template_id, region_uid))
    if kind is not None:
        c.execute("UPDATE hand_questions SET kind=? WHERE template_id=? AND region_uid=?",
                  (kind, template_id, region_uid))
    if choices is not None:
        c.execute("UPDATE hand_questions SET choices_json=? WHERE template_id=? AND region_uid=?",
                  (json.dumps([{"label": d["label"], "rect": list(d["rect"])} for d in choices]),
                   template_id, region_uid))
    c.commit(); c.close()

def list_hand_groups(template_id: int, region_uid: str) -> List[dict]:
    """[{id, label, is_blank, items, comment, members: [submission ids], auto_key, checked}],
    blank group first. auto_key: what LMGrader grouped by (None: made by hand); checked False:
    suggested by LMGrader, not checked by the human yet."""
    c = connect()
    rows = c.execute("SELECT * FROM hand_groups WHERE template_id=? AND region_uid=? ORDER BY is_blank DESC, id",
                     (template_id, region_uid)).fetchall()
    out = []
    for r in rows:
        members = [m["submission_id"] for m in c.execute(
            "SELECT submission_id FROM hand_group_members WHERE group_id=? ORDER BY submission_id", (r["id"],))]
        out.append({"id": r["id"], "label": r["label"] or "", "is_blank": bool(r["is_blank"]),
                    "items": json.loads(r["items_json"]) if r["items_json"] else [],
                    "comment": r["comment"] or "", "members": members,
                    "auto_key": r["auto_key"], "checked": r["checked"] is None or bool(r["checked"])})
    c.close()
    return out

def add_hand_group(template_id: int, region_uid: str, label: str, members: List[int], is_blank=False,
                   auto_key: str = None, checked: bool = True) -> int:
    """New group; its members leave any other group of the same question. auto_key / checked=False:
    a group LMGrader suggests (the human checks it)."""
    from datetime import datetime
    c = connect()
    cur = c.execute("INSERT INTO hand_groups(template_id,region_uid,label,is_blank,created_at,auto_key,checked) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (template_id, region_uid, label, int(is_blank), datetime.now().isoformat(timespec="seconds"),
                     auto_key, int(bool(checked))))
    gid = cur.lastrowid
    c.commit(); c.close()
    set_hand_group_members(gid, members, add=True)
    return gid

def set_hand_group_members(group_id: int, members: List[int], add: bool = False):
    """Replace (or, add=True, extend) a group's members. A test is in at most one group per
    question: added members leave their other group, and groups left empty are deleted (except
    the Blank group, which every question keeps)."""
    c = connect()
    g = c.execute("SELECT template_id, region_uid FROM hand_groups WHERE id=?", (group_id,)).fetchone()
    if g is None:
        c.close()
        return
    others = "SELECT id FROM hand_groups WHERE template_id=? AND region_uid=? AND id<>?"
    if members:
        q = ",".join("?" * len(members))
        c.execute(f"DELETE FROM hand_group_members WHERE group_id IN ({others}) AND submission_id IN ({q})",
                  (g["template_id"], g["region_uid"], group_id, *members))
    if not add:
        c.execute("DELETE FROM hand_group_members WHERE group_id=?", (group_id,))
    c.executemany("INSERT OR IGNORE INTO hand_group_members(group_id,submission_id) VALUES(?,?)",
                  [(group_id, m) for m in members])
    c.execute("DELETE FROM hand_groups WHERE template_id=? AND region_uid=? AND is_blank=0 AND id NOT IN "
              "(SELECT DISTINCT group_id FROM hand_group_members)", (g["template_id"], g["region_uid"]))
    c.commit(); c.close()

def remove_from_hand_groups(template_id: int, region_uid: str, members: List[int]):
    """Members become ungrouped; groups left empty are deleted (not the Blank group)."""
    if not members:
        return
    c = connect()
    q = ",".join("?" * len(members))
    c.execute(f"DELETE FROM hand_group_members WHERE submission_id IN ({q}) AND group_id IN "
              "(SELECT id FROM hand_groups WHERE template_id=? AND region_uid=?)", (*members, template_id, region_uid))
    c.execute("DELETE FROM hand_groups WHERE template_id=? AND region_uid=? AND is_blank=0 AND id NOT IN "
              "(SELECT DISTINCT group_id FROM hand_group_members)", (template_id, region_uid))
    c.commit(); c.close()

def update_hand_group(group_id: int, label: str = None, items: List[str] = None, comment: str = None,
                      checked: bool = None):
    c = connect()
    if checked is not None:
        c.execute("UPDATE hand_groups SET checked=? WHERE id=?", (int(bool(checked)), group_id))
    if label is not None:
        c.execute("UPDATE hand_groups SET label=? WHERE id=?", (label, group_id))
    if items is not None:
        c.execute("UPDATE hand_groups SET items_json=? WHERE id=?", (json.dumps(list(items)), group_id))
    if comment is not None:
        c.execute("UPDATE hand_groups SET comment=? WHERE id=?", (comment, group_id))
    c.commit(); c.close()

def delete_hand_group(group_id: int):
    c = connect()
    c.execute("DELETE FROM hand_group_members WHERE group_id=?", (group_id,))
    c.execute("DELETE FROM hand_groups WHERE id=?", (group_id,))
    c.commit(); c.close()

# ---- Exam settings ----
def get_exam_instructions(exam_name: str, course_id: int = None) -> str:
    c = connect()
    row = c.execute("SELECT instructions FROM course_exams WHERE course_id=? AND exam_name=?",
                    (_course(course_id), exam_name)).fetchone()
    c.close()
    return (row["instructions"] or "") if row else ""

def set_exam_instructions(exam_name: str, instructions: str, course_id: int = None):
    c = connect()
    c.execute("INSERT INTO course_exams(course_id,exam_name,instructions) VALUES(?,?,?) "
              "ON CONFLICT(course_id,exam_name) DO UPDATE SET instructions=excluded.instructions",
              (_course(course_id), exam_name, instructions))
    c.commit(); c.close()

def get_exam_strictness(exam_name: str, course_id: int = None) -> int:
    """5 (very generous) .. 10 (strict, the default)."""
    from .config import clamp_strictness
    c = connect()
    row = c.execute("SELECT strictness FROM course_exams WHERE course_id=? AND exam_name=?",
                    (_course(course_id), exam_name)).fetchone()
    c.close()
    return clamp_strictness(row["strictness"] if row else None)

def set_exam_strictness(exam_name: str, level: int, course_id: int = None):
    from .config import clamp_strictness
    c = connect()
    c.execute("INSERT INTO course_exams(course_id,exam_name,strictness) VALUES(?,?,?) "
              "ON CONFLICT(course_id,exam_name) DO UPDATE SET strictness=excluded.strictness",
              (_course(course_id), exam_name, clamp_strictness(level)))
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
