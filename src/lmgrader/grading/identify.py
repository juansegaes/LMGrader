"""Identify a scanned test: which exam version it is and which student wrote it."""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import numpy as np
from ..models import TestTemplate, Student
from ..template.alignment import read_image, align_with_score, crop_region, save_image
from ..llm.client import image_to_b64, prepare_crop
from ..students.matching import NameMatch, match_student
from ..config import MIN_ALIGN_INLIERS, CROP_PADDING, data_dir

@dataclass
class Identification:
    template: Optional[TestTemplate] = None
    aligned: Optional[np.ndarray] = None      # first page, aligned to the chosen version
    inliers: int = 0
    scores: dict = field(default_factory=dict)   # version label -> inliers
    name: Optional[NameMatch] = None
    error: str = ""
    version_read: str = ""          # "field: B", "layout", "manual", ...
    version_uncertain: bool = False  # the version field couldn't be read: a human should check

    @property
    def alignment_ok(self) -> bool:
        return self.aligned is not None and self.inliers >= MIN_ALIGN_INLIERS

_cache = {}

def _template_page(path):
    img = _cache.get(path)
    if img is None:
        img = read_image(path)
        if img is not None:
            _cache[path] = img
    return img

def crop_paths(sub_id) -> Tuple[str, str]:
    """Where the name / student-ID crops of a submission are cached for the UI."""
    nd, idd = data_dir() / "name_crops", data_dir() / "id_crops"
    nd.mkdir(parents=True, exist_ok=True)
    idd.mkdir(parents=True, exist_ok=True)
    return str(nd / f"sub_{sub_id}.png"), str(idd / f"sub_{sub_id}.png")

def detect_version(scan: np.ndarray, versions: List[TestTemplate], page: int = 0) -> Identification:
    """Align against the same page of every version; the one with the most RANSAC inliers wins."""
    best = Identification()
    for t in versions:
        if page >= t.page_count:
            continue
        t_img = _template_page(t.pages[page])
        if t_img is None:
            continue
        aligned, inl = align_with_score(t_img, scan)
        best.scores[t.version_label] = inl
        if aligned is not None and inl > best.inliers:
            best.template, best.aligned, best.inliers = t, aligned, inl
    if best.template is None and versions:
        best.error = "Could not align the scan to any version"
    return best

def _aligned_page(template: TestTemplate, scan_pages: List[str], p: int):
    scan = read_image(scan_pages[p]) if p < len(scan_pages) else None
    t_img = _template_page(template.pages[p]) if p < template.page_count else None
    if scan is None or t_img is None:
        return None
    a, _ = align_with_score(t_img, scan)
    return a if a is not None else scan

def read_version_field(llm, versions: List[TestTemplate], scan_pages: List[str],
                       ident: Identification) -> Optional[TestTemplate]:
    """Crop the version field (of the layout-detected version if it has one) and ask the LLM
    which version label it shows. None if there is no field, no LLM, or it can't tell."""
    with_field = [t for t in versions if t.version_rect]
    if llm is None or not with_field or not hasattr(llm, "read_version"):
        return None
    ref = next((t for t in with_field if ident.template is not None and t.id == ident.template.id), with_field[0])
    p = ref.version_page
    use_first = p == 0 and ident.template is not None and ident.template.id == ref.id and ident.aligned is not None
    img = ident.aligned if use_first else _aligned_page(ref, scan_pages, p)
    if img is None:
        return None
    crop = crop_region(img, ref.version_rect, pad=CROP_PADDING)
    label = llm.read_version(image_to_b64(prepare_crop(crop)), [t.version_label for t in versions])
    return next((t for t in versions if t.version_label == label), None)

def choose_version(scan: np.ndarray, versions: List[TestTemplate], scan_pages: List[str], llm=None) -> Identification:
    """Layout match (ORB) first; when the exam has a version field and several versions, what the
    field says wins. An unreadable field keeps the layout guess but marks it uncertain."""
    ident = detect_version(scan, versions)
    ident.version_read = "layout" if ident.template else ""
    if len(versions) < 2 or not any(t.version_rect for t in versions) or llm is None:
        return ident
    try:
        chosen = read_version_field(llm, versions, scan_pages, ident)
    except Exception:
        chosen = None
    if chosen is None:
        ident.version_read, ident.version_uncertain = "layout (version field unreadable)", True
        return ident
    ident.version_read = f"field: {chosen.version_label}"
    if ident.template is None or chosen.id != ident.template.id:
        t_img = _template_page(chosen.pages[0])
        aligned, inl = align_with_score(t_img, scan) if t_img is not None else (None, 0)
        ident.template, ident.aligned, ident.inliers, ident.error = chosen, aligned, inl, ""
    return ident

def field_crops(template: TestTemplate, scan_pages: List[str], first_aligned=None):
    """Crops of the name and student-ID fields (None where the template has no such field).
    Each needed page is aligned once; first_aligned may supply the already aligned page 0."""
    aligned = {}
    if first_aligned is not None:
        aligned[0] = first_aligned

    def page(p):
        if p not in aligned:
            aligned[p] = _aligned_page(template, scan_pages, p)
        return aligned[p]

    out = []
    for rect, p in ((template.name_rect, template.name_page), (template.id_rect, template.id_page)):
        img = page(p) if rect else None
        out.append(crop_region(img, rect, pad=CROP_PADDING) if img is not None else None)
    return out[0], out[1]

def read_student(llm, template: TestTemplate, scan_pages: List[str], students: List[Student],
                 first_aligned=None, save_for=None, ocr=None) -> Optional[NameMatch]:
    """Crop the name / ID fields, optionally cache the crops for submission `save_for`, and match
    the roster (ID first, name as fallback). The local handwriting reader `ocr` (TrOCR) goes
    first; the LLM is only asked when that match is not sure. Without either, only the crops
    are saved."""
    name_crop, id_crop = field_crops(template, scan_pages, first_aligned)
    if save_for is not None:
        n_path, i_path = crop_paths(save_for)
        if name_crop is not None:
            save_image(n_path, name_crop)
        if id_crop is not None:
            save_image(i_path, id_crop)
    if (llm is None and ocr is None) or not students or (name_crop is None and id_crop is None):
        return None
    b64 = lambda c: image_to_b64(prepare_crop(c)) if c is not None else None
    nm = None
    if ocr is not None:
        try:
            nm = match_student(ocr, b64(id_crop), b64(name_crop), students)
            nm.method = f"trocr {nm.method}"
        except Exception:
            nm = None
        if nm is not None and nm.student is not None and not nm.needs_confirm:
            return nm
    if llm is not None:
        try:
            return match_student(llm, b64(id_crop), b64(name_crop), students)
        except Exception:
            if nm is None:
                raise
    return nm

def read_student_name(llm, template: TestTemplate, scan_pages: List[str], students: List[Student]) -> Optional[NameMatch]:
    """Backwards-compatible wrapper."""
    return read_student(llm, template, scan_pages, students)

def identify(scan_path, versions: List[TestTemplate], students: List[Student], llm=None,
             save_crops_for=None, forced: Optional[TestTemplate] = None, ocr=None) -> Identification:
    """scan_path: a single page path or the list of page paths of one test.
    forced: the version a human picked (no detection, only alignment + name reading).
    ocr: local handwriting reader (students.trocr), tried before the LLM for names / IDs."""
    pages = [scan_path] if isinstance(scan_path, str) else list(scan_path)
    scan = read_image(pages[0]) if pages else None
    if scan is None:
        return Identification(error=f"Cannot read {pages[0] if pages else 'scan'}")
    if forced is not None:
        t_img = _template_page(forced.pages[0])
        aligned, inl = align_with_score(t_img, scan) if t_img is not None else (None, 0)
        ident = Identification(template=forced, aligned=aligned, inliers=inl, version_read="manual")
    else:
        ident = choose_version(scan, versions, pages, llm)
    if ident.template is not None and (llm is not None or ocr is not None or save_crops_for is not None):
        try:
            ident.name = read_student(llm, ident.template, pages, students, ident.aligned, save_crops_for, ocr)
        except Exception as e:
            ident.error = f"Name reading failed: {e}"
    return ident
