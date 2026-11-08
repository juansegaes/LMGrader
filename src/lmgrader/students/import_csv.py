"""Student import from CSV: the name, ID and email columns are read from the header."""
from typing import List
from ..csvtable import (Table, read_table, row_name, is_points_row, guess_column, guess_name_columns,
                        ID_WORDS, EMAIL_WORDS)
from ..models import Student
from ..db import add_student

def students_from_table(table: Table, layout: str, name_col: int, col2: int = -1,
                        id_col: int = -1, email_col: int = -1) -> List[Student]:
    """col2: the second name column of a two-column layout (see csvtable.row_name)."""
    students = []
    for row in table.rows:
        name = row_name(table, row, layout, name_col, col2)
        if not name or is_points_row(name):
            continue
        student_id = table.cell(row, id_col) if id_col >= 0 else ""
        email = table.cell(row, email_col) if email_col >= 0 else ""
        students.append(Student(name=name, student_id=student_id, email=email))
    return students

def import_csv(path: str) -> List[Student]:
    table = read_table(path)
    layout, col, col2 = guess_name_columns(table)
    id_col = guess_column(table.headers, ID_WORDS, exclude=[col, col2])
    email_col = guess_column(table.headers, EMAIL_WORDS, exclude=[col, col2, id_col])
    return students_from_table(table, layout, col, col2, id_col, email_col)

def import_and_store(path: str) -> int:
    count = 0
    for s in import_csv(path):
        add_student(s)
        count += 1
    return count
