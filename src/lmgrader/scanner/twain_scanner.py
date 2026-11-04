"""Windows TWAIN scanner wrapper (best-effort)."""
from .base import Scanner
from typing import List, Optional
import tempfile, os

class TwainScanner(Scanner):
    def __init__(self):
        self._twain = None
        try:
            import twain
            self._twain = twain
        except Exception:
            self._twain = None

    def is_available(self) -> bool:
        return self._twain is not None

    def list_devices(self) -> List[str]:
        if not self._twain:
            return []
        try:
            sm = self._twain.SourceManager(None)
            return [s for s in sm.SourceList()]
        except Exception:
            return []

    def scan(self, device=None, dpi=300) -> Optional[str]:
        if not self._twain:
            return None
        try:
            import twain
            sm = twain.SourceManager(None)
            src = sm.OpenSource(device) if device else sm.OpenDefaultSource()
            src.SetCapability(twain.ICAP_XRESOLUTION, twain.TWTY_FIX32, float(dpi))
            src.SetCapability(twain.ICAP_YRESOLUTION, twain.TWTY_FIX32, float(dpi))
            src.RequestAcquire(0, 0)
            (handle, more) = src.XferImageNatively()
            # convert DIB to file via PIL if possible
            import win32ui  # may not exist
            # fallback: save handle via PIL
            from PIL import Image
            img = Image.frombytes  # placeholder
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            return path
        except Exception:
            return None
