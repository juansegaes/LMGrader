"""Names read locally with TrOCR first; the LLM only when TrOCR's match is unsure."""
import base64

import cv2
import numpy as np
import pytest

from lmgrader.models import Student
from lmgrader.students.matching import match_student, roster_number
from lmgrader.students.trocr import clean_name

ROSTER = [Student(id=1, name="Alice Johnson", student_id="ajohns12"), Student(id=2, name="Bob Smith",
          student_id="bsmith"), Student(id=3, name="Roberta Smithers", student_id="rsmith3")]


def test_clean_name_drops_labels_digits_and_box_edges():
    assert clean_name("name : Alice Johnson") == "Alice Johnson"
    assert clean_name("I David Couch") == "David Couch"
    assert clean_name('" Hudson Rice') == "Hudson Rice"
    assert clean_name("Ryan Gwinn 1982") == "Ryan Gwinn"
    assert clean_name("Nombre: Carlos Díaz") == "Carlos Díaz"


def test_logins_are_not_student_numbers():
    assert roster_number(Student(student_id="jsmith2")) == ""
    assert roster_number(Student(student_id="801452236")) == "801452236"
    assert roster_number(Student(student_id="80 145 2236")) == "801452236"


class Reader:
    """A reader (TrOCR-like) with fixed transcriptions and no tiebreak."""
    def __init__(self, name, sid=""):
        self.name, self.sid = name, sid

    def read_name(self, b):
        return {"name": self.name, "legible": True}

    def read_student_id(self, b):
        return self.sid

    def pick_name(self, b, cands):
        return -1


def test_a_handwritten_id_does_not_block_a_roster_of_logins():
    """Rosters with logins as student_id: a clear name match is accepted even though the ID
    written on the exam can't be compared (it used to always ask the human)."""
    nm = match_student(Reader("Alice Jonson", "801452236"), "id", "name", ROSTER)
    assert nm.student.id == 1 and not nm.needs_confirm and nm.method == "fuzzy"
    unsure = match_student(Reader("R Smith"), None, "name", ROSTER)
    assert unsure.needs_confirm                       # no tiebreak: left for the LLM / human


def _b64(img):
    return base64.b64encode(cv2.imencode(".png", img)[1].tobytes()).decode()


def _written(text, w=520):
    img = np.full((90, w, 3), 255, np.uint8)
    cv2.putText(img, "Name:", (8, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    cv2.putText(img, text, (110, 60), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.4, (40, 40, 40), 2)
    cv2.line(img, (100, 72), (w - 10, 72), (0, 0, 0), 1)
    return img


@pytest.mark.trocr
def test_real_trocr_reads_and_matches_without_an_llm(world):
    from lmgrader.students import trocr
    if not trocr.available():
        pytest.skip("torch/transformers not installed")
    reader = trocr.get_reader()
    if reader is None:
        pytest.skip("TrOCR model not available (offline and not cached)")
    assert reader.read_name(_b64(_written("Alice Johnson")))["name"] == "Alice Johnson"
    nm = match_student(reader, None, _b64(_written("Bob Smith")), ROSTER)
    assert nm.student.id == 2 and not nm.needs_confirm and nm.method == "fuzzy"
