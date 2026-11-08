"""The mouse wheel only scrolls: it never changes a dropdown, number box or slider (app.NoWheelOnEntries)."""
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QScrollArea, QSlider,
                             QSpinBox, QVBoxLayout, QWidget)

from lmgrader.app import NoWheelOnEntries


def wheel(widget, down=True):
    """Send one wheel notch to `widget` (through the application, like a real wheel event)."""
    c = QPointF(widget.rect().center())
    ev = QWheelEvent(c, QPointF(widget.mapToGlobal(c.toPoint())), QPoint(0, 0), QPoint(0, -120 if down else 120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


@pytest.fixture
def page(qapp):
    """A scrollable page with one entry of each kind near the top."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.resize(400, 300)
    content = QWidget()
    lay = QVBoxLayout(content)
    entries = {"spin": QSpinBox(), "double": QDoubleSpinBox(), "slider": QSlider(Qt.Orientation.Horizontal),
               "combo": QComboBox()}
    entries["spin"].setRange(0, 10); entries["spin"].setValue(5)
    entries["double"].setRange(0, 10); entries["double"].setValue(3.0)
    entries["slider"].setRange(0, 10); entries["slider"].setValue(5)
    entries["combo"].addItems(["a", "b", "c"]); entries["combo"].setCurrentIndex(1)
    for w in entries.values():
        lay.addWidget(w)
    lay.addSpacing(2000)                      # tall page: there is something to scroll
    area.setWidget(content)
    area.show()
    qapp.processEvents()
    yield area, entries
    area.close()


def values(entries):
    return (entries["spin"].value(), entries["double"].value(), entries["slider"].value(),
            entries["combo"].currentIndex())


def test_without_guard_wheel_changes_entries(page):
    """Control: Qt's default really changes them (so the next test means something)."""
    _, entries = page
    before = values(entries)
    for w in entries.values():
        wheel(w)
    after = values(entries)
    assert all(a != b for a, b in zip(before, after))


def test_wheel_scrolls_page_instead_of_changing_entries(page, qapp):
    area, entries = page
    guard = NoWheelOnEntries()
    qapp.installEventFilter(guard)
    try:
        before = values(entries)
        bar = area.verticalScrollBar()
        for name, w in entries.items():
            pos = bar.value()
            wheel(w)
            assert values(entries) == before, f"the wheel changed the {name}"
            assert bar.value() > pos, f"the page did not scroll over the {name}"
        # the line edit inside a number box is covered too
        line = entries["spin"].findChild(QLineEdit)
        assert line is not None
        wheel(line)
        assert values(entries) == before
        # scroll bars are not entries: they keep scrolling
        pos = bar.value()
        wheel(bar)
        assert bar.value() > pos
    finally:
        qapp.removeEventFilter(guard)
