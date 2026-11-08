import cv2
import numpy as np

from lmgrader.grading.blank_cnn import BlankDetector, get_detector, is_blank, preprocess


def _white():
    return np.full((200, 600, 3), 255, np.uint8)


def _scribbled():
    img = _white()
    rng = np.random.default_rng(0)
    for _ in range(12):
        p1 = (int(rng.integers(0, 600)), int(rng.integers(0, 200)))
        p2 = (int(rng.integers(0, 600)), int(rng.integers(0, 200)))
        cv2.line(img, p1, p2, (0, 0, 0), 12)
    return img


def _gray():
    return np.full((200, 600), 255, np.uint8)


def test_predict_shape_and_range():
    p = BlankDetector().predict([_white(), _scribbled(), _gray()])
    assert p.shape == (3,)
    assert np.all((p >= 0.0) & (p <= 1.0))


def test_preprocess_path_and_array(tmp_path):
    path = tmp_path / "box.png"
    cv2.imwrite(str(path), _scribbled())
    from_path = preprocess(path)
    from_array = preprocess(_scribbled())
    for arr in (from_path, from_array):
        assert arr.shape == (128, 128, 1)
        assert arr.dtype == np.float32
        assert arr.min() >= 0.0 and arr.max() <= 1.0


def test_predict_empty_list():
    p = BlankDetector().predict([])
    assert isinstance(p, np.ndarray)
    assert p.shape == (0,)


def test_white_is_blank_and_scribbled_is_not():
    det = get_detector()
    p_white, p_scrib = det.predict([_white(), _scribbled()])
    assert is_blank(p_white)
    assert not is_blank(p_scrib)
