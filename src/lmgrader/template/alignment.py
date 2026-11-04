"""Image alignment and template helpers."""
import cv2
import numpy as np
from typing import Tuple, Optional, List

def read_image(path: str) -> Optional[np.ndarray]:
    # imdecode instead of imread so non-ASCII paths work on Windows
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except (OSError, ValueError, TypeError):
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)

def to_gray(img: np.ndarray) -> np.ndarray:
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img

def align_with_score(template: np.ndarray, scan: np.ndarray) -> Tuple[Optional[np.ndarray], int]:
    """Align scanned page to template using ORB feature matching + homography.
    Returns (warped scan in template coordinate space or None, RANSAC inlier count).
    The inlier count measures how well the scan matches this template, so it is
    also used to pick the right exam version."""
    t_gray = _prep(template)
    s_gray = _prep(scan)
    orb = cv2.ORB_create(5000)
    kp_t, des_t = orb.detectAndCompute(t_gray, None)
    kp_s, des_s = orb.detectAndCompute(s_gray, None)
    if des_t is None or des_s is None or len(kp_t) < 10 or len(kp_s) < 10:
        return None, 0
    # Lowe ratio test: drops ambiguous matches (repeated glyphs, box corners) that cross-check keeps
    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des_s, des_t, k=2)
    matches = [p[0] for p in knn if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
    if len(matches) < 10:
        return None, 0
    matches = sorted(matches, key=lambda m: m.distance)[:1500]
    src_pts = np.float32([kp_s[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp_t[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
    H, mask = cv2.findHomography(src_pts, dst_pts, method, 5.0, maxIters=5000, confidence=0.999)
    if H is None or not _plausible(H, scan.shape, template.shape):
        return None, 0
    h, w = template.shape[:2]
    warped = cv2.warpPerspective(scan, H, (w, h), borderValue=(255, 255, 255) if scan.ndim == 3 else 255)
    return warped, int(mask.sum()) if mask is not None else 0

def _prep(img: np.ndarray) -> np.ndarray:
    """Grayscale with stretched contrast, so faint scans and crisp PDF renders give similar features."""
    g = to_gray(img)
    return cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)

def _plausible(H: np.ndarray, src_shape, dst_shape) -> bool:
    """Reject degenerate homographies (a scanned page is only slightly rotated / scaled / shifted)."""
    sh, sw = src_shape[:2]
    corners = np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]).reshape(-1, 1, 2)
    try:
        out = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
    except cv2.error:
        return False
    if not cv2.isContourConvex(out.astype(np.float32)):
        return False
    area = cv2.contourArea(out.astype(np.float32))
    dh, dw = dst_shape[:2]
    return 0.5 * dw * dh <= area <= 2.0 * dw * dh

def align_scan_to_template(template: np.ndarray, scan: np.ndarray) -> Optional[np.ndarray]:
    return align_with_score(template, scan)[0]

def crop_region(img: np.ndarray, rect: Tuple[int,int,int,int], pad: int = 0) -> np.ndarray:
    x, y, w, h = rect
    x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
    h_img, w_img = img.shape[:2]
    x = max(0, min(x, w_img-1)); y = max(0, min(y, h_img-1))
    w = max(1, min(w, w_img - x)); h = max(1, min(h, h_img - y))
    return img[y:y+h, x:x+w]

def ink_ratio(img: np.ndarray, threshold: int = 128) -> float:
    g = to_gray(img)
    ink = (g < threshold).sum()
    return ink / g.size

def has_significant_ink(img: np.ndarray, threshold: int = 128, ratio: float = 0.005) -> bool:
    return ink_ratio(img, threshold) >= ratio

def draw_regions(img: np.ndarray, regions, color=(0,255,0), thickness=2) -> np.ndarray:
    out = img.copy()
    for r in regions:
        x,y,w,h = r.rect if hasattr(r,'rect') else r['rect']
        cv2.rectangle(out, (x,y), (x+w, y+h), color, thickness)
        label = getattr(r, 'problem', None) or (r.get('problem') if isinstance(r, dict) else '')
        cv2.putText(out, str(label), (x, max(15, y-5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return out

def save_image(path: str, img: np.ndarray) -> bool:
    ext = "." + path.rsplit(".", 1)[-1] if "." in path else ".png"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)
    return bool(ok)
