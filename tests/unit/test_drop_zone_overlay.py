"""The drag overlay's zone logic.

The overlay decides what a release means, so these tests concentrate on the
commits it must refuse: on the scrim, and on a Read zone drawn disabled. Getting
those wrong opens a file the user never singled out, which is exactly the guess
the two-zone design exists to avoid.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from joyread.app.launch.intent import classify_drop_paths
from joyread.infrastructure.i18n import locale_service
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.drop_zone_overlay import (
    IMPORT_ZONE,
    READ_ZONE,
    DropZoneOverlay,
)


@pytest.fixture()
def overlay(qtbot):
    locale_service.init(ResourceLoader().locale_dir(), None, "English")

    def _make() -> DropZoneOverlay:
        widget = DropZoneOverlay(ResourceLoader())
        qtbot.addWidget(widget)
        widget.resize(1200, 808)
        widget.show()
        return widget

    return _make


class _StripedWidget(QWidget):
    """High-contrast stand-in for the shelf, so a blur has something to soften."""

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        for x in range(0, self.width(), 8):
            painter.fillRect(x, 0, 4, self.height(), QColor(Qt.GlobalColor.black))
        painter.end()


def _cbz(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"not a real archive")
    return path


def _center_of(widget: DropZoneOverlay, zone: str) -> QPoint:
    return widget.zone_rects()[zone].center()


def _between_zones(widget: DropZoneOverlay) -> QPoint:
    """A point in the gutter: inside the overlay, inside neither zone."""

    rects = widget.zone_rects()
    x = (rects[READ_ZONE].right() + rects[IMPORT_ZONE].left()) // 2
    return QPoint(x, rects[READ_ZONE].center().y())


def test_dropping_on_read_emits_the_single_path(overlay, tmp_path) -> None:
    widget = overlay()
    source = _cbz(tmp_path, "Volume 01.cbz")
    widget.begin(classify_drop_paths([source]))
    seen: list[Path] = []
    widget.read_requested.connect(seen.append)

    assert widget.handle_drop(_center_of(widget, READ_ZONE))

    assert seen == [source.resolve()]


def test_dropping_on_import_emits_every_path(overlay, tmp_path) -> None:
    widget = overlay()
    first = _cbz(tmp_path, "a.cbz")
    second = _cbz(tmp_path, "b.cbz")
    widget.begin(classify_drop_paths([first, second]))
    seen: list[tuple] = []
    widget.import_requested.connect(seen.append)

    assert widget.handle_drop(_center_of(widget, IMPORT_ZONE))

    assert seen == [(first.resolve(), second.resolve())]


def test_a_disabled_read_zone_refuses_to_commit(overlay, tmp_path) -> None:
    """The load-bearing case. Two files have no single reader target, so a
    release on Read must do nothing rather than pick one."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz"), _cbz(tmp_path, "b.cbz")]))
    read_seen: list[Path] = []
    import_seen: list[tuple] = []
    widget.read_requested.connect(read_seen.append)
    widget.import_requested.connect(import_seen.append)

    assert not widget.handle_drop(_center_of(widget, READ_ZONE))

    assert read_seen == []
    assert import_seen == []


def test_a_folder_cannot_be_read_but_can_be_imported(overlay, tmp_path) -> None:
    widget = overlay()
    folder = tmp_path / "Series"
    folder.mkdir()
    widget.begin(classify_drop_paths([folder]))
    read_seen: list[Path] = []
    import_seen: list[tuple] = []
    widget.read_requested.connect(read_seen.append)
    widget.import_requested.connect(import_seen.append)

    assert not widget.handle_drop(_center_of(widget, READ_ZONE))
    assert read_seen == []

    widget.begin(classify_drop_paths([folder]))
    assert widget.handle_drop(_center_of(widget, IMPORT_ZONE))
    assert import_seen == [(folder.resolve(),)]


def test_releasing_between_the_zones_commits_nothing(overlay, tmp_path) -> None:
    """The design is explicit that the file only commits inside a zone."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    seen: list = []
    widget.read_requested.connect(seen.append)
    widget.import_requested.connect(seen.append)

    assert not widget.handle_drop(_between_zones(widget))

    assert seen == []


def test_hovering_a_disabled_read_zone_does_not_read_as_hovered(overlay, tmp_path) -> None:
    """A hover highlight would promise that releasing there does something."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz"), _cbz(tmp_path, "b.cbz")]))

    widget.update_hover(_center_of(widget, READ_ZONE))
    assert widget.hover_zone is None

    widget.update_hover(_center_of(widget, IMPORT_ZONE))
    assert widget.hover_zone == IMPORT_ZONE


def test_hover_tracks_an_enabled_read_zone(overlay, tmp_path) -> None:
    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))

    widget.update_hover(_center_of(widget, READ_ZONE))
    assert widget.hover_zone == READ_ZONE

    widget.update_hover(_between_zones(widget))
    assert widget.hover_zone is None


def test_an_unusable_payload_never_raises_the_overlay(overlay, tmp_path) -> None:
    widget = overlay()
    widget.hide()
    (tmp_path / "notes.txt").write_text("nope")

    widget.begin(classify_drop_paths([tmp_path / "notes.txt"]))

    assert not widget.isVisible()
    assert not widget.payload.can_import


def test_import_shows_the_confirmation_and_read_does_not(overlay, tmp_path) -> None:
    """Read hands off to a reader window, which is its own acknowledgement;
    Import backgrounds its work and would otherwise show nothing at all."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    widget.handle_drop(_center_of(widget, READ_ZONE))
    assert not widget.is_confirming

    widget.begin(classify_drop_paths([_cbz(tmp_path, "b.cbz")]))
    widget.handle_drop(_center_of(widget, IMPORT_ZONE))
    assert widget.is_confirming


def test_the_confirmation_clears_itself(overlay, qtbot, tmp_path) -> None:
    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    widget.handle_drop(_center_of(widget, IMPORT_ZONE))

    qtbot.waitUntil(
        lambda: not widget.is_confirming,
        timeout=Theme.drop_confirm_hold_ms + 2000,
    )

    assert widget.payload.import_paths == ()


def test_ending_a_drag_forgets_the_payload(overlay, tmp_path) -> None:
    """A stale payload would let the next drag commit the previous drop."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))

    widget.end()

    assert not widget.payload.can_import
    assert widget.hover_zone is None
    assert not widget.handle_drop(_center_of(widget, IMPORT_ZONE))


def test_the_zones_split_the_width_evenly_and_clear_the_pill(overlay) -> None:
    widget = overlay()
    rects = widget.zone_rects()

    assert rects[READ_ZONE].width() == rects[IMPORT_ZONE].width()
    assert rects[IMPORT_ZONE].left() - rects[READ_ZONE].right() - 1 == Theme.drop_zone_gap
    # The pill sits above the zones; overlapping it would put the file count
    # on top of the Read icon.
    assert rects[READ_ZONE].top() >= Theme.drop_pill_margin_top + Theme.drop_pill_height


def test_a_backdrop_source_that_cannot_be_grabbed_still_opens(overlay, tmp_path) -> None:
    """The blurred snapshot is fidelity, not function: losing it must leave a
    working flat scrim rather than a broken overlay."""

    widget = overlay()
    widget.set_content_area(None)

    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))

    assert widget.isVisible()
    assert widget.payload.can_read


def test_the_overlay_keeps_its_content_while_it_fades_out(overlay, qtbot, tmp_path) -> None:
    """Dismissal must not repaint the default state at full opacity first.

    ``end()`` starts a 160ms fade. Clearing the payload there would make the
    first frame of that fade show "1 item" and an enabled Read zone -- so the
    last thing the user sees is a flash of a drag they never made. The private
    attribute is what painting reads, which is exactly what this pins.
    """

    widget = overlay()
    widget.begin(
        classify_drop_paths(
            [_cbz(tmp_path, "a.cbz"), _cbz(tmp_path, "b.cbz"), _cbz(tmp_path, "c.cbz")]
        )
    )

    widget.end()

    assert widget.isVisible()
    assert widget._payload.item_count == 3
    # The public payload is empty straight away, so nothing can still commit.
    assert not widget.payload.can_import

    qtbot.waitUntil(lambda: not widget.isVisible(), timeout=2000)
    assert widget._payload.item_count == 0


def test_the_checkmark_survives_until_the_overlay_is_gone(overlay, qtbot, tmp_path) -> None:
    """Same reason: the confirmation must not flash back to the zones on its
    way out."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    widget.handle_drop(_center_of(widget, IMPORT_ZONE))
    assert widget.is_confirming

    widget.end()

    assert widget.isVisible()
    assert widget.is_confirming

    qtbot.waitUntil(lambda: not widget.isVisible(), timeout=2000)
    assert not widget.is_confirming


def _rendered(widget: DropZoneOverlay):
    """Paint the overlay onto white and hand back the image."""

    from PySide6.QtGui import QPixmap

    canvas = QPixmap(widget.size())
    canvas.fill(Qt.GlobalColor.white)
    widget.render(canvas, QPoint(0, 0))
    return canvas.toImage()


def test_the_scrim_leaves_the_window_s_rounded_bottom_corners_alone(overlay, tmp_path) -> None:
    """The overlay runs to the bottom edge of a window with an 18px radius.

    A square scrim paints over that curve and visibly squares off the window
    while a drag is in flight.
    """

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    widget._opacity = 1.0
    image = _rendered(widget)
    height, width = widget.height(), widget.width()

    # Well outside the arc (the corner centre sits 18px in on both axes).
    for x, y in ((1, height - 2), (width - 2, height - 2)):
        assert image.pixelColor(x, y) == QColor(Qt.GlobalColor.white), (x, y)

    # ...while the bottom edge between the corners is scrimmed as normal.
    assert image.pixelColor(width // 2, height - 2) != QColor(Qt.GlobalColor.white)
    # And the top corners stay square: they butt against the title bar.
    assert image.pixelColor(1, 1) != QColor(Qt.GlobalColor.white)


def test_hover_eases_in_rather_than_snapping(overlay, qtbot, tmp_path) -> None:
    """`Theme.drop_zone_transition_ms` is the design's 140ms zone transition.

    Fill, border, and icon scale are drawn from this progress, so a value that
    jumps straight to 1.0 means the transition is not happening at all.
    """

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    assert widget._hover_progress[READ_ZONE] == 0.0

    widget.update_hover(_center_of(widget, READ_ZONE))

    # Mid-flight: the discrete state has flipped, the drawn value has not.
    assert widget.hover_zone == READ_ZONE
    assert widget._hover_progress[READ_ZONE] < 1.0

    qtbot.waitUntil(
        lambda: widget._hover_progress[READ_ZONE] == 1.0,
        timeout=Theme.drop_zone_transition_ms + 2000,
    )

    # Leaving eases back out, and the other zone never lit up.
    widget.update_hover(_between_zones(widget))
    qtbot.waitUntil(
        lambda: widget._hover_progress[READ_ZONE] == 0.0,
        timeout=Theme.drop_zone_transition_ms + 2000,
    )
    assert widget._hover_progress[IMPORT_ZONE] == 0.0


def test_a_new_drag_starts_from_a_rest_hover_state(overlay, tmp_path) -> None:
    """Otherwise the next drag opens with the previous drag's zone still lit."""

    widget = overlay()
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    widget._hover_progress[IMPORT_ZONE] = 1.0

    widget.begin(classify_drop_paths([_cbz(tmp_path, "b.cbz")]))

    assert widget._hover_progress == {READ_ZONE: 0.0, IMPORT_ZONE: 0.0}


def test_confirming_deepens_the_blur_as_well_as_the_scrim(overlay, qtbot, tmp_path) -> None:
    """The design deepens both on confirm. The scrim colour already changed;
    this pins the blur radius actually being applied too."""

    widget = overlay()
    # Structured, not flat: blurring a uniform image at any radius returns the
    # same image, so a flat source would make this test vacuous.
    source = _StripedWidget()
    qtbot.addWidget(source)
    source.resize(400, 300)
    source.show()
    widget.set_content_area(source)

    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    dragging = widget._backdrop
    assert dragging is not None
    dragging_image = dragging.toImage()

    widget.show_import_confirmation(1)

    assert widget._backdrop is not None
    assert widget._backdrop.toImage() != dragging_image


def _striped_source(qtbot, width: int = 400, height: int = 300) -> QWidget:
    source = _StripedWidget()
    qtbot.addWidget(source)
    source.resize(width, height)
    source.show()
    return source


def test_re_entering_one_drag_reuses_the_snapshot(overlay, qtbot, tmp_path) -> None:
    """Waving a file over the window edge fires dragEnter repeatedly.

    Re-grabbing would re-render the whole shelf each time, and the hide/show
    around the grab is a real repaint that can flicker the overlay.
    """

    widget = overlay()
    widget.set_content_area(_striped_source(qtbot))
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    first = widget._snapshot
    assert first is not None

    widget.begin(classify_drop_paths([_cbz(tmp_path, "b.cbz")]))

    assert widget._snapshot is first


def test_resizing_mid_drag_recaptures_the_snapshot(overlay, qtbot, tmp_path) -> None:
    """A stale snapshot is painted stretched to the new rect, so the blurred
    shelf would look squashed until the drag ended."""

    widget = overlay()
    source = _striped_source(qtbot)
    widget.set_content_area(source)
    widget.begin(classify_drop_paths([_cbz(tmp_path, "a.cbz")]))
    first = widget._snapshot
    assert first is not None

    source.resize(600, 300)
    qtbot.waitUntil(lambda: source.width() == 600, timeout=2000)
    widget.sync_geometry()

    assert widget._snapshot is not first
    assert widget._snapshot.width() == 600
