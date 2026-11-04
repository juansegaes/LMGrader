"""Helpers for tests on the real (private) class scans in tests/fixtures/private/.

Renders are cached in tests/fixtures/private/rendered/ (git-ignored, never leaves the folder).
"""
import csv
import os
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

FIXTURES = Path(__file__).parent / "fixtures"
PRIVATE = FIXTURES / "private"
TEMPLATE_PDF = FIXTURES / "test2a.pdf"
SCANS_PDF = PRIVATE / "class_scans.pdf"
ROSTER_CSV = PRIVATE / "roster.csv"
RENDER_DIR = PRIVATE / "rendered"
DPI = 300

def available() -> bool:
    return TEMPLATE_PDF.exists() and SCANS_PDF.exists() and ROSTER_CSV.exists()

def render_pdf(pdf_path: Path, prefix: str) -> List[str]:
    """Render every page at 300 dpi (cached). Returns PNG paths in page order."""
    import pypdfium2 as pdfium
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    out = []
    for i in range(len(pdf)):
        p = RENDER_DIR / f"{prefix}_p{i + 1:03d}.png"
        if not p.exists():
            pdf[i].render(scale=DPI / 72).to_pil().convert("RGB").save(str(p))
        out.append(str(p))
    pdf.close()
    return out

def template_pages() -> List[str]:
    return render_pdf(TEMPLATE_PDF, "template")

def scan_pages() -> List[str]:
    return render_pdf(SCANS_PDF, "scan")

def find_header_boxes(page_path: str) -> Tuple[Optional[tuple], Optional[tuple]]:
    """(name_rect, id_rect): the two wide, short rectangles in the top third of page 1
    (left one = Name, right one = Student ID). Found with OpenCV, no LLM."""
    img = cv2.imread(page_path, cv2.IMREAD_GRAYSCALE)
    h, w = img.shape
    top = img[: h // 3]
    bw = cv2.threshold(top, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, bw_, bh = cv2.boundingRect(c)
        if bw_ > 0.3 * w and 0.02 * h < bh < 0.08 * h and bw_ / bh > 5:
            boxes.append((x, y, bw_, bh))
    # dedupe inner/outer contours of the same box
    boxes.sort(key=lambda b: (b[0], b[1]))
    uniq = []
    for b in boxes:
        if not any(abs(b[0] - u[0]) < 20 and abs(b[1] - u[1]) < 20 for u in uniq):
            uniq.append(b)
    uniq.sort(key=lambda b: b[0])
    if len(uniq) < 2:
        return (uniq[0] if uniq else None), None
    return uniq[0], uniq[-1]

def load_roster():
    from lmgrader.models import Student
    with open(ROSTER_CSV, newline="", encoding="utf-8") as f:
        return [Student(name=r["name"], student_id=r["student_id"], email=r["email"]) for r in csv.DictReader(f)]

def expected_names() -> List[str]:
    """The 20 test-takers in scan order (= order of tests in class_scans.pdf)."""
    return ["David Krige", "Addy Tran", "Lonnette Fullard", "Mian Ali Asghar", "Ryan Gwinn",
            "Christian Koito", "Santi Brenes-Mendez", "Jose Esquit", "Connor Josefsberg", "Dylan Holbach-Bache",
            "Duncan McDonald", "Aashwin Shankar", "Nicholas Camargo", "Sejal Grover", "Madhav Annachi",
            "DeShawn Sturgeon Jr.", "Michael Dibiacomo", "Giampaul Zeballos", "Zoey Thompson", "Kolby Carpenter"]
