"""Figma-derived popup menus for bookshelf actions."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEventLoop, QPoint, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QCursor, QHideEvent, QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QSizePolicy, QVBoxLayout, QWidget

from joyread.core.models.book import Book
from joyread.ui.resources.styles.theme import Theme


class MenuItem(QFrame):
    """A clickable menu row with Figma's 2px padding and hover fill."""

    clicked = QtSignal()

    def __init__(
        self,
        text: str,
        *,
        destructive: bool = False,
        enabled: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._enabled = enabled
        self._pressed_inside = False

        self.setObjectName("FigmaMenuItem")
        self.setProperty("destructive", "true" if destructive else "false")
        self.setProperty("menuEnabled", "true" if enabled else "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Theme.menu_item_padding,
            Theme.menu_item_padding,
            Theme.menu_item_padding,
            Theme.menu_item_padding,
        )
        layout.setSpacing(Theme.menu_item_text_gap)

        label = QLabel(text)
        label.setProperty("class", "FigmaMenuItemText")
        label.setProperty("destructive", "true" if destructive else "false")
        label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
        layout.addWidget(label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._enabled and event.button() == Qt.MouseButton.LeftButton:
            self._pressed_inside = True
            event.accept()
            return
        self._pressed_inside = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._enabled and event.button() == Qt.MouseButton.LeftButton and self._pressed_inside:
            self._pressed_inside = False
            event.accept()
            if self.rect().contains(event.position().toPoint()):
                # Closing a Qt.Popup during mousePress can leave Qt's cursor and
                # hover bookkeeping stale until the next click. Trigger after
                # release so the normal press/release sequence can complete.
                QTimer.singleShot(0, self.clicked.emit)
            return
        self._pressed_inside = False
        super().mouseReleaseEvent(event)


class FigmaMenu(QWidget):
    """Popup root containing a styled panel and a zero-margin option list."""

    closed = QtSignal()

    def __init__(self, parent: QWidget, width: int = Theme.menu_width) -> None:
        super().__init__(parent)
        self._menu_width = width
        self.setObjectName("FigmaMenuPopup")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self._loop: QEventLoop | None = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._panel = QFrame()
        self._panel.setObjectName("FigmaMenuPanel")
        self._panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._panel.setFixedWidth(self._menu_width)
        root_layout.addWidget(self._panel)

        panel_layout = QVBoxLayout(self._panel)
        # Qt borders consume layout space. A 4px layout margin plus the 2px
        # border gives the requested 6px visual inset to the option list.
        panel_layout.setContentsMargins(
            Theme.menu_layout_margin,
            Theme.menu_layout_margin,
            Theme.menu_layout_margin,
            Theme.menu_layout_margin,
        )
        panel_layout.setSpacing(0)
        panel_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        self._option_list = QWidget()
        self._option_list.setObjectName("FigmaMenuOptionList")
        self._option_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        panel_layout.addWidget(self._option_list)

        self._option_layout = QVBoxLayout(self._option_list)
        self._option_layout.setContentsMargins(0, 0, 0, 0)
        self._option_layout.setSpacing(Theme.menu_option_gap)
        self._option_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

    def exec(self, global_pos: QPoint) -> None:
        self.refresh_size()
        self.move(global_pos)
        self.show()
        self.raise_()
        self.activateWindow()
        self._loop = QEventLoop(self)
        self._loop.exec()
        self._loop = None
        _refresh_cursor_shape()

    def add_item(
        self,
        text: str,
        on_triggered: Callable[[], None] | None,
        *,
        destructive: bool = False,
        enabled: bool = True,
    ) -> MenuItem:
        item = MenuItem(text, destructive=destructive, enabled=enabled)
        if enabled and on_triggered is not None:
            item.clicked.connect(lambda callback=on_triggered: self._trigger(callback))
        self._option_layout.addWidget(item)
        self.refresh_size()
        return item

    def refresh_size(self) -> None:
        # Let Qt derive menu height from the option-list content. Width is the
        # only fixed value because the Figma component is explicitly 130px wide.
        self._option_layout.invalidate()
        self._option_list.adjustSize()
        self._panel.adjustSize()
        self.adjustSize()
        panel_height = self._panel.sizeHint().height()
        self._panel.setFixedSize(self._menu_width, panel_height)
        self.setFixedSize(self._menu_width, panel_height)
        self.updateGeometry()

    def _trigger(self, callback: Callable[[], None]) -> None:
        self.close()
        callback()

    def hideEvent(self, event: QHideEvent) -> None:
        _release_popup_grabs(self)
        if self._loop is not None and self._loop.isRunning():
            self._loop.quit()
        self.closed.emit()
        _refresh_cursor_shape()
        super().hideEvent(event)


def _figma_menu(parent: QWidget, width: int = Theme.menu_width) -> FigmaMenu:
    return FigmaMenu(parent, width=width)


def _refresh_cursor_shape() -> None:
    """Nudge Qt to recompute the cursor after popup mouse capture ends."""

    QTimer.singleShot(0, lambda: QCursor.setPos(QCursor.pos()))


def _release_popup_grabs(root: QWidget) -> None:
    """Release grabs owned by this popup before Qt recalculates hover targets."""

    for grabber_getter, release_name in (
        (QWidget.mouseGrabber, "releaseMouse"),
        (QWidget.keyboardGrabber, "releaseKeyboard"),
    ):
        grabber = grabber_getter()
        if grabber is not None and (grabber is root or root.isAncestorOf(grabber)):
            getattr(grabber, release_name)()


def build_book_context_menu(
    parent: QWidget,
    book: Book,
    on_read: Callable[[str], None],
    on_favourite: Callable[[str], None],
    on_detail: Callable[[str], None],
    on_add_to_collection: Callable[[str], None],
    on_remove: Callable[[str], None],
    on_delete: Callable[[str], None],
    *,
    show_remove: bool = True,
) -> FigmaMenu:
    menu = _figma_menu(parent)

    menu.add_item("Read", lambda: on_read(book.uuid))
    menu.add_item("Unfavourite" if book.is_favourite else "Favourite", lambda: on_favourite(book.uuid))
    menu.add_item("Detail", lambda: on_detail(book.uuid))
    menu.add_item("Add to...", lambda: on_add_to_collection(book.uuid))
    if show_remove:
        menu.add_item("Remove", lambda: on_remove(book.uuid))
    menu.add_item("Delete", lambda: on_delete(book.uuid), destructive=True)

    return menu


def build_action_menu(
    parent: QWidget,
    on_open_book: Callable[[], None],
    on_open_and_import: Callable[[], None],
    on_import: Callable[[], None],
) -> FigmaMenu:
    menu = _figma_menu(parent)

    menu.add_item("Open Book", on_open_book)
    menu.add_item("Open & Import", on_open_and_import)
    menu.add_item("Import", on_import)

    return menu
