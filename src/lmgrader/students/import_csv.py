"""Student import from CSV."""
import csv
from typing import List
from ..models import Student
from ..db import add_student

def import_csv(path: str) -> List[Student]:
    students = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # flexible column names
            name = row.get("name") or row.get("Name") or row.get("student_name") or ""
            sid = row.get("student_id") or row.get("id") or row.get("ID") or ""
            email = row.get("email") or row.get("Email") or ""
            if not name.strip():
                continue
            s = Student(name=name.strip(), student_id=str(sid).strip(), email=email.strip())
            students.append(s)
    return students

def import_and_store(path: str) -> int:
    count = 0
    for s in import_csv(path):
        add_student(s)
        count += 1
    return count
