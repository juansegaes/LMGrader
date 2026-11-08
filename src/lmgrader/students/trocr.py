"""Handwritten names and student IDs read locally with TrOCR (microsoft/trocr-base-handwritten).

TrOCR is the first reader for every test, whether or not an LLM is configured: it runs on a
laptop CPU (about a second per field), so professors without an LLM still get names matched.
The model is downloaded from Hugging Face on first use (~1.3 GB, cached in the HF cache).

TrocrReader has the reader interface students.matching.match_student expects (read_name,
read_student_id, pick_name), so the same ID-first / fuzzy-name logic runs on its transcriptions.
It never breaks a tie between roster candidates (pick_name returns -1): an unsure match is left
for the LLM fallback or for the human.
"""
import base64
import re
import threading
from typing import Optional, Tuple

import cv2
import numpy as np

from ..config import NAMES_CONFIG

_LABEL_NAME = re.compile(r"^\s*(student'?s?\s*)?(full\s*)?(name|nombre|nom)\s*[:.\-_]*\s*", re.I)
_LABEL_ID = re.compile(r"^\s*(student\s*)?(id|i\.d\.|no\.?|number|n[uú]mero|#)\s*[:.\-_#]*\s*", re.I)
# letters TrOCR reads for handwritten digits
_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "i": "1",
                                   "|": "1", "!": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "G": "6",
                                   "b": "6", "T": "7", "B": "8", "g": "9", "q": "9"})

def available() -> bool:
    """torch + transformers are installed (the model itself may still need downloading)."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))

def clean_field(img: np.ndarray) -> np.ndarray:
    """A name/ID field crop prepared for TrOCR, which reads one line of text: printed lines
    (the box border, the line to write on) are erased and the crop is cut to the ink."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img.copy()
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = ink.shape
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, w // 3), 1)))
    lines |= cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h * 2 // 3))))
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    out = gray.copy()
    out[lines > 0] = 255
    ink[lines > 0] = 0
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))   # specks
    ys, xs = np.nonzero(ink)
    if len(xs) < 20:          # (nearly) empty field
        return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    pad = max(6, h // 10)
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + pad)
    return cv2.cvtColor(out[y0:y1, x0:x1], cv2.COLOR_GRAY2BGR)

def clean_name(text: str) -> str:
    """What TrOCR read in a name field, without the printed label, digits (a date or ID written
    next to the name) and stray marks read from the box edge ("I David Couch", '" Hudson Rice')."""
    s = _LABEL_NAME.sub("", text or "")
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[\"'`()\[\]{}|!.,:;_]+(?=\s|$)|(?<=\s)[\"'`()\[\]{}|!.,:;_]+|^[\"'`()\[\]{}|!.,:;_]+", " ", s)
    words = s.split()
    while words and len(words[0]) == 1 and words[0] in "IlJ|1" and len(words) > 1:
        words.pop(0)          # the left edge of the box
    while words and len(words[-1]) == 1 and len(words) > 1:
        words.pop()           # a stray mark after the name
    return " ".join(words).strip(" -")

def _decode(image_b64: str) -> Optional[np.ndarray]:
    buf = np.frombuffer(base64.b64decode(image_b64), np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)

def _confidence(torch, out) -> float:
    """Mean probability of the generated tokens (beam search: exp of the sequence score)."""
    scores = getattr(out, "sequences_scores", None)
    if scores is not None:
        return float(max(0.0, min(1.0, float(torch.exp(scores[0])))))
    steps = getattr(out, "scores", None) or []
    probs = [float(torch.softmax(s[0], -1).max()) for s in steps]
    return sum(probs) / len(probs) if probs else 0.0

class TrocrReader:
    """Lazily loaded TrOCR model (CPU). Thread-safe: one transcription at a time."""
    num_beams = 1      # greedy: ~2x faster than beam search, same matches after fuzzy matching

    def __init__(self, model_id: str = None):
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        self.model_id = model_id or NAMES_CONFIG.get("trocr_model") or "microsoft/trocr-base-handwritten"
        self.processor = TrOCRProcessor.from_pretrained(self.model_id)
        self.model = VisionEncoderDecoderModel.from_pretrained(self.model_id)
        self.model.eval()
        self._lock = threading.Lock()

    def read(self, img: np.ndarray) -> Tuple[str, float]:
        """(text, confidence 0-1) of one handwritten line (BGR image)."""
        import torch
        from PIL import Image
        img = clean_field(img)
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        with self._lock, torch.inference_mode():
            pixels = self.processor(images=pil, return_tensors="pt").pixel_values
            out = self.model.generate(pixels, max_new_tokens=40, num_beams=self.num_beams,
                                      output_scores=True, return_dict_in_generate=True)
        text = self.processor.batch_decode(out.sequences, skip_special_tokens=True)[0].strip()
        return text, _confidence(torch, out)

    # ---- reader interface of students.matching.match_student (images as base64 PNG) ----
    def read_name(self, image_b64: str) -> dict:
        img = _decode(image_b64) if image_b64 else None
        if img is None:
            return {"name": "", "legible": False}
        text, conf = self.read(img)
        name = clean_name(text)
        return {"name": name, "legible": bool(name) and conf >= 0.2}

    def read_student_id(self, image_b64: str) -> str:
        img = _decode(image_b64) if image_b64 else None
        if img is None:
            return ""
        text, _ = self.read(img)
        text = _LABEL_ID.sub("", text).translate(_DIGIT_LOOKALIKES)
        return re.sub(r"\D", "", text)

    def pick_name(self, image_b64: str, candidates: list) -> int:
        return -1      # no tiebreak: an unsure match goes to the LLM fallback or the human

_reader: Optional[TrocrReader] = None
_reader_lock = threading.Lock()

def get_reader() -> Optional[TrocrReader]:
    """The shared reader (loaded, and downloaded the first time, on first use); None when
    torch/transformers are missing or the model can't be loaded."""
    global _reader
    with _reader_lock:
        if _reader is None and available():
            try:
                _reader = TrocrReader()
            except Exception:
                import traceback
                traceback.print_exc()
                return None
        return _reader
