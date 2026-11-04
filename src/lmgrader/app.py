"""Application factory."""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QApplication, QComboBox, QWidget
from .ui.main_window import MainWindow
from .ui.theme import apply_theme

class NoWheelOnCombos(QObject):
    """App-wide: the mouse wheel never changes a closed dropdown; the wheel event goes to the
    widget behind it instead (so the page still scrolls). An open dropdown list scrolls as usual."""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QWidget):
            combo = obj if isinstance(obj, QComboBox) else obj.parent()
            if isinstance(combo, QComboBox):       # the combo itself or its line edit (editable combos)
                parent = combo.parentWidget()
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
    app.wheel_guard = NoWheelOnCombos(app)  # keep reference
    app.installEventFilter(app.wheel_guard)
    w = MainWindow()
    w.show()
    app.main_window = w  # keep reference
    return app
