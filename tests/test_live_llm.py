"""Live tests against a running LM Studio server. Skipped when it isn't reachable.

Model: env LMGRADER_TEST_MODEL, else LLM_CONFIG["model"] if the server has it,
else qwen3-vl-8b-instruct. Each call can take 10-60 s on a local GPU.
"""
import json
import os
import urllib.request

import cv2
import numpy as np
import pytest

from lmgrader.config import LLM_CONFIG
from lmgrader.llm.client import LLMClient, image_to_b64, prepare_crop
from lmgrader.models import Student
from lmgrader.students.matching import match_name

DEFAULT_MODEL = "qwen3-vl-8b-instruct"
VISION_HINTS = ("vl", "vision", "gemma-3", "gemma-4", "llava", "pixtral", "minicpm-v")

def _available_models():
    try:
        with urllib.request.urlopen(LLM_CONFIG["base_url"].rstrip("/") + "/models", timeout=3) as r:
            return [m["id"] for m in json.load(r).get("data", [])]
    except Exception:
        return None

def _pick_model(models):
    want = os.environ.get("LMGRADER_TEST_MODEL")
    if want:
        return want if want in models else None
    if LLM_CONFIG.get("model") in models and any(h in LLM_CONFIG["model"].lower() for h in VISION_HINTS):
        return LLM_CONFIG["model"]
    if DEFAULT_MODEL in models:
        return DEFAULT_MODEL
    return next((m for m in models if any(h in m.lower() for h in VISION_HINTS)), None)

_models = _available_models()
_model = _pick_model(_models) if _models else None
pytestmark = pytest.mark.skipif(_model is None,
                                reason="LM Studio not reachable or no vision model loaded")

def text_img(lines, w=900, scale=1.0, font=cv2.FONT_HERSHEY_SIMPLEX):
    img = np.full((60 + 50 * len(lines), w, 3), 255, np.uint8)
    for i, l in enumerate(lines):
        cv2.putText(img, l, (20, 50 + 50 * i), font, scale, (0, 0, 0), 2)
    return img

HAND = cv2.FONT_HERSHEY_SCRIPT_SIMPLEX

@pytest.fixture(scope="module")
def llm():
    return LLMClient(base_url=LLM_CONFIG["base_url"], api_key=LLM_CONFIG["api_key"],
                     model=_model, max_tokens=2048)

@pytest.fixture(scope="module")
def question(llm):
    img = text_img(["3. (4 pts) Solve x^2 - 5x + 6 = 0 for x.", "   Show your work."])
    q = llm.extract_question(image_to_b64(prepare_crop(img)))
    print("\nQUESTION:", q)
    return q

@pytest.fixture(scope="module")
def rubric(llm, question):
    r = llm.generate_rubric(question, 4, "3")
    print("\nRUBRIC:\n" + r.as_text(4))
    return r

def test_extract_question_latex(question):
    assert "x^2" in question.replace(" ", "") or "x^{2}" in question.replace(" ", "")
    assert "$" in question or "\\(" in question or "\\[" in question

def test_generate_rubric(rubric):
    assert rubric.expected_answer and rubric.items
    assert rubric.as_text(4).startswith("Expected answer:") and "- [" in rubric.as_text(4)

def test_grade_correct_answer(llm, question, rubric):
    img = text_img(["(x-2)(x-3) = 0", "x = 2 or x = 3"], 600, 1.3, HAND)
    g = llm.grade_answer(image_to_b64(prepare_crop(img)), "3", "algebra", rubric, 4, question=question)
    print("\nGRADE correct:", g)
    assert g["review_reason"] != "llm_error"
    assert g["score"] >= 3

def test_grade_wrong_answer(llm, question, rubric):
    img = text_img(["x = 5 + 6", "x = 11"], 600, 1.3, HAND)
    g = llm.grade_answer(image_to_b64(prepare_crop(img)), "3", "algebra", rubric, 4, question=question)
    print("\nGRADE wrong:", g)
    assert g["review_reason"] != "llm_error"
    assert g["score"] <= 1

def test_name_matching(llm):
    roster = [Student(id=i, name=n) for i, n in
              enumerate(["Alice Johnson", "Bob Smith", "Roberta Smithers", "Carlos Diaz"])]
    img = text_img(["Name: Roberta Smithers"], 700, 1.3, HAND)
    m = match_name(llm, image_to_b64(prepare_crop(img)), roster)
    print("\nNAME:", m)
    assert m.student is not None and m.student.name == "Roberta Smithers"
