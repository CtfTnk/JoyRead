"""The approved progress material must keep its alpha and endpoint geometry."""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QEnterEvent
from PySide6.QtWidgets import QApplication, QWidget

from joyread.core.reader import ReaderDirection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.reader_controls import ReaderProgressSlider


@pytest.fixture
def progress(qtbot):
    host = QWidget()
    host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    host.resize(240, 12)
    slider = ReaderProgressSlider(host)
    slider.setGeometry(0, 0, 240, 12)
    slider.setRange(0, 100)
    slider.setValue(35)
    slider.set_reading_direction(ReaderDirection.LEFT_TO_RIGHT)
    qtbot.addWidget(host)
    host.show()
    QApplication.processEvents()
    QApplication.sendEvent(slider, QEvent(QEvent.Type.Leave))
    return host, slider


def pixel(host, x, y):
    pixmap = host.grab()
    ratio = pixmap.devicePixelRatio()
    return pixmap.toImage().pixelColor(int((x + 0.5) * ratio), int((y + 0.5) * ratio))


@pytest.mark.parametrize("profile", ("quiet", "contrast"))
@pytest.mark.parametrize("direction", list(ReaderDirection))
def test_progress_rails_keep_design_alpha_without_overlap(progress, profile, direction):
    host, slider = progress
    host.setProperty("glassBackdrop", profile)
    slider.set_reading_direction(direction)
    palette = Theme.reader_progress_palettes[profile]
    read_x, unread_x = (200, 40) if direction == ReaderDirection.RIGHT_TO_LEFT else (40, 200)
    for x, expected in ((read_x, palette.read), (unread_x, palette.unread)):
        actual = pixel(host, x, 6).getRgb()
        assert max(abs(a - b) for a, b in zip(actual, expected, strict=True)) <= 1
    # The rail never shows through the white knob.
    assert pixel(host, slider._handle_center_x(), 6) == QColor("#ffffff")


@pytest.mark.parametrize("direction", list(ReaderDirection))
@pytest.mark.parametrize("value", (0, 35, 100))
def test_progress_endpoint_knobs_and_segments_stay_inside_allocation(progress, direction, value):
    host, slider = progress
    slider.set_reading_direction(direction)
    slider.setValue(value)
    handle = slider._handle_rect()
    assert handle.size().toTuple() == (22, 10)
    assert 0 <= handle.left() < handle.right() <= slider.width()
    assert handle.top() == 1 and handle.bottom() == 11
    track, read, unread = slider._track_rect(), slider._filled_track_rect(), slider._unfilled_track_rect()
    assert track.height() == 6
    assert read.width() + unread.width() == pytest.approx(track.width())
    assert read.intersected(unread).isEmpty()
    assert read.width() == pytest.approx(track.width() * value / 100)
    center = slider._handle_center_x()
    assert pixel(host, center, 6) == QColor("#ffffff")
    # The horizontal tips are visible at both endpoints; their stroke is not
    # clipped by a pen centered outside the widget's left/right edge.
    assert pixel(host, handle.left(), 6).alpha() > 0
    assert pixel(host, handle.right() - 1, 6).alpha() > 0
    assert pixel(host, center, 0).alpha() == 0
    assert pixel(host, center, 11).alpha() == 0


def test_progress_hover_drag_and_silent_release_keep_knob_size(progress):
    host, slider = progress
    geometry = slider._handle_rect(), slider._track_rect(), slider.size()
    center = slider._handle_center_x()
    normal_edge = pixel(host, center, 1)
    # QWidget's Enter handler requires QEnterEvent on the shipping Qt 6.8 leg.
    position = QPointF(center, slider.height() / 2)
    QApplication.sendEvent(slider, QEnterEvent(position, position, position))
    hover_edge = pixel(host, center, 1)
    assert hover_edge.lightness() < normal_edge.lightness()
    assert pixel(host, center, 6) == QColor("#ffffff")
    slider.setSliderDown(True)
    assert pixel(host, center, 6) == QColor("#f5f5f5")
    assert pixel(host, center, 1).lightness() < hover_edge.lightness()
    slider.blockSignals(True)
    slider.setSliderDown(False)
    slider.blockSignals(False)
    assert pixel(host, center, 1) == hover_edge
    QApplication.sendEvent(slider, QEvent(QEvent.Type.Leave))
    assert pixel(host, center, 1) == normal_edge
    assert (slider._handle_rect(), slider._track_rect(), slider.size()) == geometry


def test_empty_progress_range_retains_a_complete_knob(progress):
    host, slider = progress
    slider.setRange(0, 0)
    assert slider._filled_track_rect().isEmpty()
    assert slider._unfilled_track_rect() == slider._track_rect()
    assert slider._handle_rect().size().toTuple() == (22, 10)
    assert pixel(host, slider._handle_center_x(), 6) == QColor("#ffffff")
