"""Drag-and-drop wiring on the main window.

The overlay's own tests cover which release means what. These drive real Qt drag
events through ``MainWindow`` instead, because the parts that only exist at the
seam -- accepting or refusing the drag, mapping window coordinates into the
overlay, and reaching the reader and import pipelines -- have no other coverage.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, QUrl, Qt
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import QWidget

from joyread.app.app_context import create_app_context
from joyread.ui.views.main_window import MainWindow
from joyread.ui.widgets.drop_zone_overlay import IMPORT_ZONE, READ_ZONE


@pytest.fixture()
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(tmp_path / "runtime"))
    context = create_app_context()
    main = None
    try:
        launches: list[object] = []
        main = MainWindow(context, standalone_reader_launcher=launches.append)
        # Not qtbot.addWidget: that destroys the window before this fixture's
        # own teardown runs, and the context has to outlive it.
        main.resize(1200, 860)
        main.show()
        # The overlay is sized from the content panel, needing a layout pass.
        qtbot.waitUntil(lambda: main.drop_zone_overlay.width() > 0, timeout=2000)
        yield main, launches
    finally:
        # Without this, a failure constructing MainWindow leaks an open SQLite
        # connection per test instead of producing one readable error.
        if main is not None:
            main.close()
        context.close()


class _FakeEmbeddedReader(QWidget):
    """The part of ``EmbeddedReaderShell`` that MainWindow teardown touches."""

    def cancel(self) -> None:
        return None


def _cbz(path: Path, color: str = "#336699") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = path.with_suffix(".png")
    Image.new("RGB", (10, 20), color).save(image, format="PNG")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.write(image, "001.png")
    image.unlink()
    return path


def _mime(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def _enter(main: MainWindow, mime: QMimeData, point: QPoint) -> QDragEnterEvent:
    event = QDragEnterEvent(
        point,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main.dragEnterEvent(event)
    return event


def _zone_point(main: MainWindow, zone: str) -> QPoint:
    """A point in window coordinates that lands inside *zone*."""

    overlay = main.drop_zone_overlay
    return overlay.mapTo(main, overlay.zone_rects()[zone].center())


def _drop(main: MainWindow, mime: QMimeData, zone: str) -> QDropEvent:
    event = QDropEvent(
        QPointF(_zone_point(main, zone)),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main.dropEvent(event)
    return event


def test_dragging_a_book_in_raises_the_overlay(window, tmp_path) -> None:
    main, _launches = window
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))

    event = _enter(main, mime, QPoint(600, 400))

    assert event.isAccepted()
    assert main.drop_zone_overlay.isVisible()
    assert main.drop_zone_overlay.payload.can_read


def test_dragging_something_unsupported_is_refused_before_any_ui(window, tmp_path) -> None:
    """Refusing the drag is what makes the OS show a no-drop cursor. Raising the
    overlay and then declining the release would be a worse answer."""

    main, _launches = window
    notes = tmp_path / "notes.txt"
    notes.write_text("nope")

    event = _enter(main, _mime(notes), QPoint(600, 400))

    assert not event.isAccepted()
    assert not main.drop_zone_overlay.isVisible()


def test_a_drag_is_refused_while_a_dialog_is_up(window, tmp_path) -> None:
    """A drop landing behind a modal acts on a window the user cannot see."""

    main, _launches = window
    main.dialog_overlay.show_info("Busy", "Something is already asking.")
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))

    event = _enter(main, mime, QPoint(600, 400))

    assert not event.isAccepted()
    assert not main.drop_zone_overlay.isVisible()


def test_dropping_on_read_opens_a_reader_without_importing(window, tmp_path) -> None:
    main, launches = window
    source = _cbz(tmp_path / "drop" / "a.cbz")
    mime = _mime(source)
    _enter(main, mime, QPoint(600, 400))

    event = _drop(main, mime, READ_ZONE)

    assert event.isAccepted()
    assert len(launches) == 1
    # Read must not touch the library.
    assert main._context.book_repository.list_books() == []


def test_dropping_on_import_submits_an_import_and_opens_no_reader(
    window, tmp_path, monkeypatch
) -> None:
    main, launches = window
    submitted: list[str] = []
    # Imports stream progress, so they go through ``submit_stream`` rather than
    # ``submit`` -- the drop path has to reach the same submission as every
    # other import entry point, not a second one that skips the progress dialog.
    original = main._context.task_service.submit_stream

    def _record(name, work, **kwargs):  # noqa: ANN001, ANN202
        submitted.append(name)
        return original(name, work, **kwargs)

    monkeypatch.setattr(main._context.task_service, "submit_stream", _record)
    first = _cbz(tmp_path / "drop" / "a.cbz", "#111111")
    second = _cbz(tmp_path / "drop" / "b.cbz", "#222222")
    mime = _mime(first, second)
    _enter(main, mime, QPoint(600, 400))

    event = _drop(main, mime, IMPORT_ZONE)

    assert event.isAccepted()
    assert submitted == ["import-dropped"]
    assert launches == []


def test_dropping_two_files_on_read_does_nothing(window, tmp_path) -> None:
    """End-to-end version of the disabled-zone rule: the window must refuse the
    release, not open one of the two files."""

    main, launches = window
    mime = _mime(
        _cbz(tmp_path / "drop" / "a.cbz", "#111111"),
        _cbz(tmp_path / "drop" / "b.cbz", "#222222"),
    )
    _enter(main, mime, QPoint(600, 400))
    assert not main.drop_zone_overlay.payload.can_read

    event = _drop(main, mime, READ_ZONE)

    assert not event.isAccepted()
    assert launches == []


def test_leaving_the_window_dismisses_the_overlay(window, qtbot, tmp_path) -> None:
    main, _launches = window
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))
    _enter(main, mime, QPoint(600, 400))
    assert main.drop_zone_overlay.isVisible()

    main.dragLeaveEvent(QDragLeaveEvent())

    qtbot.waitUntil(lambda: not main.drop_zone_overlay.isVisible(), timeout=2000)


def test_a_drop_near_a_zone_edge_is_mapped_into_overlay_coordinates(window, tmp_path) -> None:
    """The overlay sits below the title bar, so window coordinates are offset
    from its own. Dropping at a zone's centre survives that error; dropping near
    an edge does not, which is what makes the mapping load-bearing here.
    """

    main, launches = window
    overlay = main.drop_zone_overlay
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))
    _enter(main, mime, QPoint(600, 400))
    # Guard against this test going vacuous if the overlay ever becomes
    # coincident with the window. The overlay takes its geometry when the drag
    # begins, so this can only be asked afterwards.
    assert overlay.mapTo(main, QPoint(0, 0)).y() > 0

    zone = overlay.zone_rects()[READ_ZONE]
    near_bottom = overlay.mapTo(main, QPoint(zone.center().x(), zone.bottom() - 10))
    event = QDropEvent(
        QPointF(near_bottom),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main.dropEvent(event)

    assert event.isAccepted()
    assert len(launches) == 1


def test_a_drag_is_refused_while_the_settings_page_is_open(window, tmp_path) -> None:
    """Settings covers the whole content area. It is not modal in Qt's sense,
    but it is opaque from the user's side, which is what matters."""

    main, _launches = window
    main._show_settings_page()
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))

    event = _enter(main, mime, QPoint(600, 400))

    assert not event.isAccepted()
    assert not main.drop_zone_overlay.isVisible()


def test_a_drop_is_refused_when_a_dialog_opens_mid_drag(window, tmp_path) -> None:
    """A background import finishing mid-drag raises its summary dialog over
    the overlay; releasing then must not commit behind it."""

    main, launches = window
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))
    _enter(main, mime, QPoint(600, 400))
    assert main.drop_zone_overlay.isVisible()

    main.dialog_overlay.show_info("Import finished", "3 books imported.")
    event = _drop(main, mime, READ_ZONE)

    assert not event.isAccepted()
    assert launches == []


def test_a_drag_is_refused_while_a_book_is_open_in_the_embedded_reader(window, tmp_path) -> None:
    """The embedded reader fills the content area, so a drop behind it would
    import or open against a library the user is not looking at.

    A stand-in is enough: the guard's rule is that an embedded reader exists at
    all, and building a real one needs an imported book. It carries ``cancel``
    because window teardown calls it on whatever shell is installed.
    """

    main, _launches = window
    main._embedded_reader = _FakeEmbeddedReader(main)
    mime = _mime(_cbz(tmp_path / "drop" / "a.cbz"))

    event = _enter(main, mime, QPoint(600, 400))

    assert not event.isAccepted()
    assert not main.drop_zone_overlay.isVisible()
