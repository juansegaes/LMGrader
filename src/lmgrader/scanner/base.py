"""Scanner abstraction."""
from abc import ABC, abstractmethod
from typing import List, Optional
import os

class Scanner(ABC):
    @abstractmethod
    def list_devices(self) -> List[str]: ...
    @abstractmethod
    def scan(self, device: Optional[str] = None, dpi: int = 300) -> Optional[str]:
        """Scan one page, save to temp file, return path or None."""
        ...
    @abstractmethod
    def is_available(self) -> bool: ...

def get_scanner() -> Scanner:
    import sys
    if sys.platform == "win32":
        for mod, cls in [("wia_scanner", "WiaScanner"), ("twain_scanner", "TwainScanner")]:
            try:
                m = __import__(f".{mod}", fromlist=[cls], package="lmgrader.scanner")
                s = getattr(m, cls)()
                if s.is_available():
                    return s
            except Exception:
                pass
    # SANE for linux/mac
    try:
        from .sane_scanner import SaneScanner
        s = SaneScanner()
        if s.is_available():
            return s
    except Exception:
        pass
    from .image_input import ImageInputScanner
    return ImageInputScanner()
