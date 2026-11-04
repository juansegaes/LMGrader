"""Shared test setup.

LMGRADER_DATA must point at a throwaway directory *before* lmgrader is imported,
because db.connect() migrates once per process (db._migrated). We use a single
session-wide data dir and seed it with a legacy (v0.1) database so the migration
path is exercised on first connect.
"""
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

# Each run's data (DB, aligned pages, crops) lands in tests/_last_run/<timestamp>-<pid> so it can
# be inspected; only the 10 most recent runs are kept (generous, so parallel runs never lose
# their directory mid-run).
_RUNS = Path(__file__).parent / "_last_run"
_RUNS.mkdir(exist_ok=True)
for _old in sorted(_RUNS.iterdir())[:-9]:
    shutil.rmtree(_old, ignore_errors=True)
DATA_DIR = _RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
DATA_DIR.mkdir()
os.environ["LMGRADER_DATA"] = str(DATA_DIR)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

SCREENSHOT_DIR = Path(__file__).parent / "screenshots"

# ---- legacy (v0.1) schema, migrated on first db.connect() ----
_con = sqlite3.connect(DATA_DIR / "lmgrader.db")
_con.executescript("""
CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, image_path TEXT NOT NULL,
 width INTEGER, height INTEGER, regions_json TEXT);
CREATE TABLE submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, template_id INTEGER,
 scan_front_path TEXT, scan_back_path TEXT, aligned_front_path TEXT, has_work_on_back INTEGER DEFAULT 0,
 back_reviewed INTEGER DEFAULT 0);
CREATE TABLE grades (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, region_id INTEGER, problem TEXT,
 grp TEXT, score REAL, max_points REAL, feedback TEXT, needs_human_review INTEGER DEFAULT 0, review_reason TEXT,
 extracted_image_path TEXT);
INSERT INTO templates(name,image_path,width,height,regions_json) VALUES('Old quiz','x.png',10,10,
 '[{"problem":"Q1","group":"g","rect":[0,0,5,5],"points":2,"rubric":"r"}]');
""")
_con.commit()
_con.close()

import numpy as np  # noqa: E402
import cv2  # noqa: E402

class FakeLLM:
    """Stands in for LLMClient; records calls."""
    def __init__(self, read=""):
        self.read, self.picks = read, 0

    def read_name(self, b):
        return {"name": self.read, "legible": True}

    def pick_name(self, b, cands):
        self.picks += 1
        return cands.index("Bob Smith") if "Bob Smith" in cands else -1

    def extract_question(self, b):
        return r"Solve $x^2-4=0$."

    def extract_part(self, b, number, letter):
        return {"stem": rf"Let $f(x)=x^2-4$ (question {number}).", "part": f"Part {letter}: solve $f(x)=0$."}

    def generate_rubric(self, q, p, prob, instructions="", notes=""):
        return f"Expected answer: x=\\pm2\n- [{p:g} pt] both roots"

    def grade_answer(self, b, problem, group, rubric, pts, question="", instructions="", notes="", **kw):
        assert "x^2" in question and "both roots" in rubric
        return {"score": 3, "feedback": "ok", "needs_human_review": False, "review_reason": "",
                "transcription": "x=2,-2"}

def make_page(seed, text):
    """Synthetic exam page with enough texture for ORB alignment."""
    rng = np.random.default_rng(seed)
    img = np.full((1100, 850, 3), 255, np.uint8)
    for _ in range(60):
        x, y = rng.integers(20, 800), rng.integers(20, 1050)
        cv2.putText(img, chr(rng.integers(65, 90)) * 3, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    cv2.putText(img, text, (60, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
    return img

@pytest.fixture(scope="session")
def data_dir():
    return DATA_DIR

@pytest.fixture(scope="session")
def world():
    """Exam 'Midterm' with versions A and B, a rotated scan of version B, and a roster."""
    from lmgrader import db
    from lmgrader.models import Student
    from lmgrader.template.alignment import save_image
    from lmgrader.template.template import create_template_from_image, add_region, duplicate_as_version, persist

    pa, pb = str(DATA_DIR / "a.png"), str(DATA_DIR / "b.png")
    save_image(pa, make_page(1, "VERSION A"))
    save_image(pb, make_page(2, "VERSION B"))
    ta = create_template_from_image("Mid vA", pa, "Midterm", "A")
    r = add_region(ta, "Q1", "algebra", (100, 300, 300, 150), 4)
    r.question_rect = (100, 200, 400, 80)
    ta.name_rect = (500, 100, 300, 60)
    ta.id = persist(ta)
    tb = duplicate_as_version(ta, pb, "B")
    tb.id = persist(tb)

    scan = make_page(2, "VERSION B")
    M = cv2.getRotationMatrix2D((425, 550), 2.0, 0.97)
    scan = cv2.warpAffine(scan, M, (850, 1100), borderValue=(255, 255, 255))
    sp = str(DATA_DIR / "scan.png")
    save_image(sp, scan)

    for n in ["Alice Johnson", "Bob Smith", "Roberta Smithers", "Carlos Díaz"]:
        db.add_student(Student(name=n))
    return {"pa": pa, "pb": pb, "scan": sp, "ta_id": ta.id, "tb_id": tb.id}

@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    from lmgrader.ui.theme import apply_theme
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app
