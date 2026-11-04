"""Windows WIA (Windows Image Acquisition) scanner wrapper."""
from .base import Scanner
from typing import List, Optional
import tempfile, os

class WiaScanner(Scanner):
    def __init__(self):
        self._wia = None
        try:
            import win32com.client
            self._wia = win32com.client
        except Exception:
            self._wia = None

    def is_available(self) -> bool:
        if self._wia is None:
            return False
        try:
            dev = self._wia.Dispatch("WIA.DeviceManager")
            return True
        except Exception:
            return False

    def list_devices(self) -> List[str]:
        if not self._wia:
            return []
        try:
            dm = self._wia.Dispatch("WIA.DeviceManager")
            out = []
            for i in range(1, dm.DeviceInfos.Count + 1):
                di = dm.DeviceInfos(i)
                if di.Type == 1:  # scanner
                    out.append(di.Properties("Name").Value)
            return out
        except Exception:
            return []

    def scan(self, device=None, dpi=300) -> Optional[str]:
        if not self._wia:
            return None
        try:
            dm = self._wia.Dispatch("WIA.DeviceManager")
            target = None
            for i in range(1, dm.DeviceInfos.Count + 1):
                di = dm.DeviceInfos(i)
                if di.Type == 1:
                    if device is None or di.Properties("Name").Value == device:
                        target = di
                        break
            if target is None:
                return None
            dev = target.Connect()
            item = dev.Items(1)
            # set resolution
            for prop_name, val in (("Horizontal Resolution", dpi), ("Vertical Resolution", dpi)):
                try:
                    item.Properties(prop_name).Value = dpi
                except Exception:
                    pass
            img = item.Transfer("{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}")  # BMP
            fd, path = tempfile.mkstemp(suffix=".bmp")
            os.close(fd)
            img.SaveFile(path)
            return path
        except Exception:
            return None
