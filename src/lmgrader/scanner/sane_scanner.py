"""Linux SANE scanner wrapper."""
from .base import Scanner
from typing import List, Optional
import tempfile, os

class SaneScanner(Scanner):
    def __init__(self):
        self._sane = None
        try:
            import sane
            sane.init()
            self._sane = sane
        except Exception:
            self._sane = None

    def is_available(self) -> bool:
        return self._sane is not None

    def list_devices(self) -> List[str]:
        if not self._sane:
            return []
        try:
            return [d[0] for d in self._sane.get_devices()]
        except Exception:
            return []

    def scan(self, device=None, dpi=300) -> Optional[str]:
        if not self._sane:
            return None
        try:
            dev = self._sane.open(device) if device else self._sane.open(self._sane.get_devices()[0][0])
            dev.resolution = dpi
            dev.mode = 'color'
            dev.start()
            img = dev.snap()
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img.save(path)
            dev.close()
            return path
        except Exception:
            return None
