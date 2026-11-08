"""Application factory."""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import (QApplication, QAbstractSlider, QAbstractSpinBox, QComboBox, QScrollBar,
                             QWidget)
from .ui.main_window import MainWindow
from .ui.theme import apply_theme

def _wheel_entry(obj):
    """The value entry a wheel event over `obj` would change (dropdown, number box, slider), or
    None. Scroll bars are not entries: they keep scrolling."""
    for w in (obj, obj.parent()):          # the entry itself or its line edit (editable combos, spin boxes)
        if isinstance(w, (QComboBox, QAbstractSpinBox)):
            return w
        if isinstance(w, QAbstractSlider) and not isinstance(w, QScrollBar):
            return w
    return None

class NoWheelOnEntries(QObject):
    """App-wide: the mouse wheel only scrolls. It never changes a closed dropdown, a number box
    (scores, points…) or a slider; the event goes to the widget behind it instead, so the page
    still scrolls. An open dropdown list scrolls as usual (its popup is not an entry)."""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QWidget):
            entry = _wheel_entry(obj)
            if entry is not None:
                parent = entry.parentWidget()
                while parent is not None:          # like Qt's own propagation: first ancestor that takes it
                    event.ignore()
                    QApplication.sendEvent(parent, event)
                    if event.isAccepted():
                        break
                    parent = parent.parentWidget()
                return True
        return False

def create_app():
    app = QApplication([])
    app.setApplicationName("LMGrader")
    apply_theme(app)
    app.wheel_guard = NoWheelOnEntries(app)  # keep reference
    app.installEventFilter(app.wheel_guard)
    w = MainWindow()
    w.show()
    app.main_window = w  # keep reference
    return app
