"""Exports for returning tests through an LMS (bulk feedback upload + gradebook CSV).

Presets:
  generic - ZIP of PDFs named by a pattern + grades.csv
  canvas  - Canvas gradebook import CSV (Student, ID, SIS User ID, Section, "<exam> (points)")
            + ZIP of PDFs named by SIS id (attach in SpeedGrader / per-student comments)
  moodle  - "Upload multiple feedback files in a zip": one folder per student
            "<Full Name>_<id>_assignsubmission_file_/" + offline grading worksheet CSV
"""
import csv
import os
import re
import zipfile
from typing import Callable, List, Optional
from ..db import add_return
from .items import ReturnItem, collect_items, ensure_pdf, split_name

PRESETS = {
    "generic": "Generic (ZIP + grades.csv)",
    "canvas": "Canvas (gradebook CSV + ZIP)",
    "moodle": "Moodle (feedback files ZIP + grading worksheet)",
}
DEFAULT_PATTERN = "{student_id}_{last}_{first}.pdf"

def _safe(s: str, keep_spaces=False) -> str:
    pat = r"[^\w\- ]+" if keep_spaces else r"[^\w\-]+"
    s = re.sub(pat, "_", s or "", flags=re.UNICODE).strip("_ ")
    return s[:100] or "untitled"

def _fmt(v: float) -> str:
    return f"{v:g}"

def _fields(item: ReturnItem) -> dict:
    name = item.student.name if item.student else (item.sub.name_read or f"unassigned {item.sub.id}")
    first, last = split_name(name)
    return {"student_id": (item.student.student_id if item.student else "") or f"sub{item.sub.id}",
            "first": first, "last": last, "name": name, "exam": item.exam, "version": item.version,
            "sub_id": item.sub.id}

def filename_for(item: ReturnItem, pattern: str = DEFAULT_PATTERN) -> str:
    raw = (pattern or DEFAULT_PATTERN).format_map(_fields(item))
    stem = raw[:-4] if raw.lower().endswith(".pdf") else raw
    return _safe(stem) + ".pdf"

def export_lms(exam_name: str, out_dir: str, preset: str = "generic", pattern: str = DEFAULT_PATTERN,
               items: Optional[List[ReturnItem]] = None, include_needs_review: bool = False,
               progress: Callable[[int, int, str], None] = None,
               make_pdf: Callable[[ReturnItem], str] = ensure_pdf) -> dict:
    """Write the ZIP + CSV for an LMS. Returns {zip, csv, files, warnings}."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset {preset!r}")
    os.makedirs(out_dir, exist_ok=True)
    if items is None:
        items = collect_items(exam_name, include_needs_review)
    warnings = []
    unassigned = [it for it in items if it.student is None]
    if unassigned:
        warnings.append(f"{len(unassigned)} test(s) have no student assigned; they are named by submission id.")
    if preset == "moodle":
        warnings.append("Moodle matches feedback folders by its own participant id. The roster's student_id "
                        "is used here; if Moodle rejects the zip, download its grading worksheet to map ids.")
    if preset == "canvas":
        warnings.append("Canvas: import the CSV in Gradebook → Import. Attach the PDFs per student in "
                        "SpeedGrader (Canvas has no bulk feedback-file upload).")

    base = _safe(exam_name)
    zip_path = os.path.join(out_dir, f"{base}_{preset}_feedback.zip")
    csv_path = os.path.join(out_dir, {"generic": f"{base}_grades.csv",
                                      "canvas": f"{base}_canvas_gradebook.csv",
                                      "moodle": f"{base}_moodle_grades.csv"}[preset])
    files, used = [], set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, it in enumerate(items):
            f = _fields(it)
            if progress:
                progress(i, len(items), f"Exporting {f['name']}…")
            pdf = make_pdf(it)
            if preset == "moodle":
                full = f"{f['first']} {f['last']}".strip()     # Moodle shows "First Last"
                folder = f"{_safe(full, keep_spaces=True)}_{_safe(f['student_id'])}_assignsubmission_file_"
                arc = f"{folder}/{_safe(exam_name)}_feedback.pdf"
            elif preset == "canvas":
                arc = f"{_safe(f['student_id'])}.pdf"
            else:
                arc = filename_for(it, pattern)
            if arc in used:
                arc = arc[:-4] + f"_{it.sub.id}.pdf"
            used.add(arc)
            z.write(pdf, arc)
            files.append(arc)
            add_return(it.sub.id, "lms", zip_path, "exported", "", pdf)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        if preset == "canvas":
            w.writerow(["Student", "ID", "SIS User ID", "Section", f"{exam_name} (points)"])
            w.writerow(["Points Possible", "", "", "", _fmt(max((it.max for it in items), default=0))])
            for it in items:
                f = _fields(it)
                w.writerow([f"{f['last']}, {f['first']}" if f["last"] else f["first"], "",
                            it.student.student_id if it.student else "", "", _fmt(it.total)])
        elif preset == "moodle":
            w.writerow(["Identifier", "Full name", "Email address", "Grade", "Maximum Grade", "Feedback comments"])
            for it in items:
                f = _fields(it)
                w.writerow([f"Participant {f['student_id']}", f["name"], it.email, _fmt(it.total), _fmt(it.max),
                            "See attached feedback PDF."])
        else:
            w.writerow(["name", "student_id", "email", "total", "max", "percent", "pdf"])
            for it, arc in zip(items, files):
                f = _fields(it)
                w.writerow([f["name"], it.student.student_id if it.student else "", it.email,
                            _fmt(it.total), _fmt(it.max), _fmt(it.percent), arc])
    if progress:
        progress(len(items), len(items), f"Exported {len(items)} tests")
    return {"zip": zip_path, "csv": csv_path, "files": files, "warnings": warnings}
