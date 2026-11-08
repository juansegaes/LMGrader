"""Blank-answer detector: a NumPy port of Blank_cnn/cnn_blank_neg.keras.

The network predicts p(not_blank) for a scanned answer box (128x128 grayscale).
Weights live in assets/blank_cnn.npz; regenerate them with
scripts/convert_blank_cnn.py after retraining the Keras model. Inference needs
only NumPy, Pillow and OpenCV (no Keras, torch or h5py).
"""

import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "blank_cnn.npz"
SIZE = 128
THRESHOLD = 0.5


def preprocess(img) -> np.ndarray:
    """Return a (128, 128, 1) float32 array in [0, 1].

    `img` is a file path, a BGR uint8 array (HxWx3) or a grayscale array (HxW).
    Matches the reference predict_blank.py preprocessing.
    """
    if isinstance(img, (str, Path)):
        pil = Image.open(img).convert("L")
    else:
        arr = np.asarray(img)
        if arr.ndim == 3:
            rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb).convert("L")
        else:
            pil = Image.fromarray(arr)
    pil = pil.resize((SIZE, SIZE), Image.BILINEAR)
    return (np.asarray(pil, dtype=np.float32)[..., None] / 255.0).astype(np.float32)


def _conv_relu(x: np.ndarray, w: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Valid 3x3 cross-correlation (Keras Conv2D) followed by ReLU.

    x: (N, H, W, C); w: (kh, kw, C, O); b: (O,). Sums shifted slices, one per kernel tap.
    """
    kh, kw = w.shape[0], w.shape[1]
    oh, ow = x.shape[1] - kh + 1, x.shape[2] - kw + 1
    out = np.broadcast_to(b, (x.shape[0], oh, ow, w.shape[3])).astype(np.float32)
    for i in range(kh):
        for j in range(kw):
            out = out + x[:, i:i + oh, j:j + ow, :] @ w[i, j]
    return np.maximum(out, 0.0)


def _maxpool2(x: np.ndarray) -> np.ndarray:
    """MaxPool2D(2, stride 2, valid): odd sizes are floored."""
    n, h, w, c = x.shape
    h2, w2 = h // 2, w // 2
    x = x[:, :h2 * 2, :w2 * 2, :]
    return x.reshape(n, h2, 2, w2, 2, c).max(axis=(2, 4))


class BlankDetector:
    def __init__(self, path=MODEL_PATH):
        with np.load(path) as data:
            self._w = [(data[f"conv{i}_w"], data[f"conv{i}_b"]) for i in range(4)]
            self._dense_w = data["dense_w"]
            self._dense_b = data["dense_b"]

    def _forward(self, batch: np.ndarray) -> np.ndarray:
        x = batch
        for i, (w, b) in enumerate(self._w):
            x = _conv_relu(x, w, b)
            if i < 3:
                x = _maxpool2(x)
        x = x.max(axis=(1, 2))  # GlobalMaxPooling2D
        logits = x @ self._dense_w + self._dense_b
        return 1.0 / (1.0 + np.exp(-logits[:, 0]))

    def predict(self, images, batch_size: int = 64) -> np.ndarray:
        """Return p(not_blank) for each image (path or OpenCV array), shape (N,)."""
        images = list(images)
        if not images:
            return np.zeros((0,), dtype=np.float32)
        out = []
        for start in range(0, len(images), batch_size):
            chunk = images[start:start + batch_size]
            batch = np.stack([preprocess(img) for img in chunk]).astype(np.float32)
            out.append(self._forward(batch))
        return np.concatenate(out).astype(np.float32)


def is_blank(p: float, threshold: float = THRESHOLD) -> bool:
    return bool(p < threshold)


_detector = None
_detector_lock = threading.Lock()


def get_detector() -> BlankDetector:
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = BlankDetector()
        return _detector
