"""Write each student's total of one exam into a column of the user's own CSV (e.g. a Canvas
gradebook export). Other cells are left exactly as they were."""
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple
from rapidfuzz import fuzz
from ..csvtable import Table, row_name, norm_name, is_points_row
from .items import ReturnItem

CLOSE_MARGIN = 5   # a close-name match must beat the second best candidate by more than this

@dataclass
class FillResult:
    written: List[tuple] = field(default_factory=list)      # (row index, ReturnItem, "id"/"name"/"close name")
    no_test: List[str] = field(default_factory=list)        # CSV student rows with no graded test
    not_in_csv: List[ReturnItem] = field(default_factory=list)   # graded tests whose student is not in the CSV
    duplicates: List[str] = field(default_factory=list)     # student names with 2+ graded tests

def _id_values(student) -> Set[str]:
    vals = {(student.student_id or "").strip().lower(), (student.email or "").strip().lower()}
    return {v for v in vals if v}

def _words_equal(a: str, b: str) -> bool:
    return sorted(a.split()) == sorted(b.split())

def fill_grades(table: Table, items: List[ReturnItem], grade_col: int, layout: str, name_col: int,
                col2: int = -1, id_col: int = -1, min_score: float = 90) -> FillResult:
    """col2: the second name column of a two-column layout (see csvtable.row_name)."""
    if not 0 <= grade_col < len(table.headers):
        raise ValueError(f"Grade column {grade_col} is not in the CSV ({len(table.headers)} columns).")
    result = FillResult()

    # one student may have several graded tests: those are never written
    by_student: Dict[int, List[ReturnItem]] = {}
    for it in items:
        if it.student is None:
            result.not_in_csv.append(it)
        else:
            by_student.setdefault(it.student.id, []).append(it)
    duplicate_ids: Set[int] = set()
    for sid, its in by_student.items():
        if len(its) > 1:
            duplicate_ids.add(sid)
            result.duplicates.append(its[0].student.name)

    # candidate CSV rows: every row with a student name (the "Points Possible" row is not one)
    rows: Dict[int, Tuple[str, str]] = {}   # row index -> (row name, normalized name)
    for r, row in enumerate(table.rows):
        name = row_name(table, row, layout, name_col, col2)
        if name and not is_points_row(name):
            rows[r] = (name, norm_name(name))

    matched: Dict[int, Tuple[int, str]] = {}   # row index -> (student id, how)
    matched_students: Set[int] = set()

    def run_pass(how: str, accepts):
        """Row by row, in table order: the first unmatched student that `accepts(row, student)`."""
        for r in rows:
            if r in matched:
                continue
            for sid in by_student:
                if sid in matched_students:
                    continue
                if accepts(r, by_student[sid][0].student):
                    matched[r] = (sid, how)
                    matched_students.add(sid)
                    break

    if id_col >= 0:
        def by_id(r, student):
            cell = table.cell(table.rows[r], id_col).lower()
            return bool(cell) and cell in _id_values(student)
        run_pass("id", by_id)

    def by_name(r, student):
        row_norm = rows[r][1]
        stu_norm = norm_name(student.name)
        return bool(row_norm and stu_norm) and (row_norm == stu_norm or _words_equal(row_norm, stu_norm))
    run_pass("name", by_name)

    # close names: the best unmatched student, if clearly better than the next one
    for r in rows:
        if r in matched:
            continue
        row_norm = rows[r][1]
        if not row_norm:
            continue
        scored = []
        for sid in by_student:
            if sid in matched_students:
                continue
            stu_norm = norm_name(by_student[sid][0].student.name)
            if stu_norm:
                scored.append((fuzz.token_sort_ratio(row_norm, stu_norm), sid))
        if not scored:
            break
        scored.sort(reverse=True)
        best_score, best_sid = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        if best_score >= min_score and best_score - second > CLOSE_MARGIN:
            matched[r] = (best_sid, "close name")
            matched_students.add(best_sid)

    # write the matched unique students, in table order
    for r in sorted(rows):
        if r not in matched:
            result.no_test.append(rows[r][0])
            continue
        sid, how = matched[r]
        if sid in duplicate_ids:
            continue   # left unchanged, and not "no test": the student has tests
        item = by_student[sid][0]
        table.set_cell(r, grade_col, f"{item.total:g}")
        result.written.append((r, item, how))

    # students with no matching row: their tests are not in the CSV
    for sid, its in by_student.items():
        if sid not in matched_students:
            result.not_in_csv.extend(its)
    return result
