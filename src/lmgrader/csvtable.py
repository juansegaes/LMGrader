"""CSV files whose columns the user picks: rosters to import, gradebooks to write grades into.

The first row is the header. Rows are kept exactly as read (other columns, empty rows, the
delimiter, the encoding), so a gradebook written back differs only in the cells that were set.
"""
import csv
import io
import unicodedata
from dataclasses import dataclass, field
from typing import List, Sequence

NAME_FIRST_LAST, NAME_LAST_FIRST = "first_last", "last_first"
NAME_TWO_COLUMNS, NAME_TWO_LAST_FIRST = "two", "two_last_first"   # two columns: col, col2 in this order
NAME_LAYOUTS = {
    NAME_FIRST_LAST: "One column: First Last",
    NAME_LAST_FIRST: "One column: Last, First",
    NAME_TWO_COLUMNS: "Two columns: First name, Last name",
    NAME_TWO_LAST_FIRST: "Two columns: Last name, First name",
}

def two_columns(layout: str) -> bool:
    return layout in (NAME_TWO_COLUMNS, NAME_TWO_LAST_FIRST)

# header words, most specific first (guess_column tries every exact match before any partial one)
ID_WORDS = ["student_id", "student id", "sis user id", "sis login id", "login id", "student number",
            "username", "user id", "login", "id"]
EMAIL_WORDS = ["email", "e-mail", "email address", "mail"]
NAME_WORDS = ["name", "student name", "student", "full name"]
FIRST_WORDS = ["first name", "first", "given name", "firstname", "first_name"]
LAST_WORDS = ["last name", "last", "surname", "family name", "lastname", "last_name"]

@dataclass
class Table:
    headers: List[str]
    rows: List[List[str]] = field(default_factory=list)   # data rows as read (lengths may differ)
    delimiter: str = ","
    encoding: str = "utf-8"
    bom: bool = False

    def cell(self, row: Sequence[str], col: int) -> str:
        return row[col].strip() if 0 <= col < len(row) else ""

    def set_cell(self, r: int, col: int, value: str):
        row = self.rows[r]
        if col >= len(row):
            row.extend([""] * (col + 1 - len(row)))
        row[col] = value

    def column_title(self, col: int) -> str:
        """'C: Student ID' (spreadsheet letter + header) for column pickers."""
        h = self.headers[col].strip() if col < len(self.headers) else ""
        return f"{column_letter(col)}: {h or '(no header)'}"

def column_letter(col: int) -> str:
    s, n = "", col + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

def read_table(path: str) -> Table:
    """Read a CSV: UTF-8 (with or without BOM), else Windows-1252; delimiter , ; or tab."""
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delim = ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    if not rows:
        raise ValueError("The CSV file is empty.")
    return Table(headers=[h.strip() for h in rows[0]], rows=rows[1:], delimiter=delim,
                 encoding="utf-8" if enc == "utf-8-sig" else enc, bom=bom)

def write_table(path: str, table: Table):
    """Write the header + rows back with the table's delimiter and encoding (UTF-8 with BOM if
    some text doesn't fit the original encoding: nothing is turned into "?")."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=table.delimiter)
    w.writerow(table.headers)
    w.writerows(table.rows)
    text = buf.getvalue()
    try:
        data = text.encode("utf-8-sig" if table.bom else table.encoding)
    except UnicodeEncodeError:
        data = text.encode("utf-8-sig")
    with open(path, "wb") as f:
        f.write(data)

def _header_key(h: str) -> str:
    return " ".join(h.strip().lower().replace("_", " ").split())

def guess_column(headers: Sequence[str], words: Sequence[str], exclude: Sequence[int] = ()) -> int:
    """Index of the header that is one of `words` (in their order), else the first one that
    contains one of them; -1 if none."""
    keys = [_header_key(h) for h in headers]
    wkeys = [_header_key(w) for w in words]
    for w in wkeys:
        for i, k in enumerate(keys):
            if i not in exclude and k == w:
                return i
    for w in wkeys:
        for i, k in enumerate(keys):
            if i in exclude:
                continue
            if (w in k) if " " in w else (w in k.split()):     # one word: a whole word of the header
                return i
    return -1

def guess_name_columns(table: Table):
    """(layout, name column, second name column or -1). Two columns when the headers say first
    and last name, in the file's order ("Last name, First name" when the last name comes first);
    else one column (the first column unless a header says name), "Last, First" when most of its
    values have a comma."""
    first = guess_column(table.headers, FIRST_WORDS)
    last = guess_column(table.headers, LAST_WORDS, exclude=[first])
    if first >= 0 and last >= 0:
        return (NAME_TWO_LAST_FIRST, last, first) if last < first else (NAME_TWO_COLUMNS, first, last)
    col = guess_column(table.headers, NAME_WORDS)
    col = 0 if col < 0 else col
    values = [table.cell(r, col) for r in table.rows if table.cell(r, col)]
    commas = sum("," in v for v in values)
    return (NAME_LAST_FIRST if values and commas > len(values) / 2 else NAME_FIRST_LAST), col, -1

def row_name(table: Table, row: Sequence[str], layout: str, col: int, col2: int = -1) -> str:
    """The row's student name as "First Last". Two columns: col and col2 are the first and the
    second name column of the layout (NAME_TWO_LAST_FIRST: col = last name, col2 = first name)."""
    if two_columns(layout):
        first, last = (col2, col) if layout == NAME_TWO_LAST_FIRST else (col, col2)
        return " ".join(p for p in (table.cell(row, first), table.cell(row, last)) if p)
    v = " ".join(table.cell(row, col).split())
    if layout == NAME_LAST_FIRST and "," in v:
        last, first = [p.strip() for p in v.split(",", 1)]
        return f"{first} {last}".strip()
    return v

def norm_name(s: str) -> str:
    """Lower case, no accents, punctuation as spaces, single spaces: names compare equal however
    they were typed."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())

def is_points_row(name: str) -> bool:
    """Canvas gradebooks have a "Points Possible" row under the header: not a student."""
    return norm_name(name).startswith("points possible")
