"""Fallback scanner that imports image files from disk, plus page import helpers."""
from .base import Scanner
from typing import List, Optional
from pathlib import Path
import shutil, uuid

IMAGE_FILTER = "Images & PDFs (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.pdf)"

class ImageInputScanner(Scanner):
    def is_available(self) -> bool:
        return True
    def list_devices(self) -> List[str]:
        return ["File import"]
    def scan(self, device=None, dpi=300) -> Optional[str]:
        # This scanner does not perform hardware scan; UI handles file dialog.
        return None

def import_pages(path: str, subdir: str = "scans", progress=None, should_stop=None) -> List[str]:
    """Copy an image (or render every page of a PDF) into the data dir. Returns PNG/image paths.
    progress(done, total) is called before each PDF page and at the end; should_stop() ends the
    import early (the pages rendered so far are returned)."""
    from ..config import data_dir
    out_dir = data_dir() / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(path)
    if src.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(src))
        out = []
        try:
            n = len(pdf)
            for i in range(n):
                if should_stop and should_stop():
                    break
                if progress:
                    progress(i, n)
                img = pdf[i].render(scale=300 / 72).to_pil()  # 300 dpi
                p = out_dir / f"{src.stem}_p{i + 1}_{uuid.uuid4().hex[:6]}.png"
                img.save(str(p))
                out.append(str(p))
            if progress:
                progress(len(out), n)
        finally:
            pdf.close()
        return out
    dst = out_dir / f"{src.stem}_{uuid.uuid4().hex[:6]}{src.suffix.lower()}"
    shutil.copyfile(src, dst)
    return [str(dst)]
