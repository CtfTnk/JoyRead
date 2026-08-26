"""Figma-derived popup menus for bookshelf actions."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEventLoop, QPoint, QSize, QTimer, Qt, Signal as QtSignal
from PySide6.QtGui import QCursor, QHideEvent, QIcon, QMouseEvent, QTransform
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from joyread.core.models.book import Book
from joyread.core.models.language import Language
from joyread.infrastructure.i18n.locale_service import book_language_display_name, t
from joyread.infrastructure.resources.resource_loader import ResourceLoader
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
        selected: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._enabled = enabled
        self._pressed_inside = False

        self.setObjectName("FigmaMenuItem")
        self.setProperty("destructive", "true" if destructive else "false")
        self.setProperty("menuEnabled", "true" if enabled else "false")
        self.setProperty("selected", "true" if selected else "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.setFixedHeight(Theme.menu_item_height)
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
        # Deliberately unparented, and kept in a local for the whole call. A
        # QEventLoop parented to this widget is destroyed with it, and a menu
        # can be destroyed by the very action it triggered. That leaves a
        # dangling pointer in QThreadData::eventLoops, which
        # QCoreApplication::exit() walks calling exit() on every entry -- so
        # the crash lands later, on quit, far from the menu that caused it.
        loop = QEventLoop()
        self._loop = loop
        try:
            loop.exec()
        finally:
            if self._loop is loop:
                self._loop = None
        _refresh_cursor_shape()

    def add_item(
        self,
        text: str,
        on_triggered: Callable[[], None] | None,
        *,
        destructive: bool = False,
        enabled: bool = True,
        selected: bool = False,
    ) -> MenuItem:
        item = MenuItem(text, destructive=destructive, enabled=enabled, selected=selected)
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
        # close() only asks the loop to quit; it returns long before exec()
        # does. Invoking the callback here would run the entire action nested
        # inside this menu's own event loop, so each menu-driven step stacks
        # another loop on the main thread and the menu cannot be torn down
        # while its own exec() is still on the stack. Deferring runs the action
        # after exec() has returned, at the caller's own stack depth.
        self.close()
        QTimer.singleShot(0, callback)

    def hideEvent(self, event: QHideEvent) -> None:
        _release_popup_grabs(self)
        if self._loop is not None and self._loop.isRunning():
            self._loop.quit()
        self.closed.emit()
        _refresh_cursor_shape()
        super().hideEvent(event)


class LanguageDropdownMenu(QWidget):
    """Figma dropdown-menu component with top/bottom indicators and hidden scrollbars."""

    closed = QtSignal()

    def __init__(
        self,
        parent: QWidget,
        resources: ResourceLoader,
        width: int = Theme.language_menu_width,
    ) -> None:
        super().__init__(parent)
        self._resources = resources
        self._menu_width = width
        self._item_count = 0
        self._loop: QEventLoop | None = None
        self.setObjectName("LanguageDropdownMenuPopup")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._panel = QFrame()
        self._panel.setObjectName("LanguageDropdownMenuPanel")
        self._panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._panel.setFixedWidth(self._menu_width)
        root_layout.addWidget(self._panel)

        panel_layout = QVBoxLayout(self._panel)
        # Figma uses 6px horizontal and 4px vertical visual padding around a
        # 2px stroke. Qt borders consume layout space, so subtract the stroke.
        panel_layout.setContentsMargins(
            Theme.language_menu_layout_margin_horizontal,
            Theme.language_menu_layout_margin_vertical,
            Theme.language_menu_layout_margin_horizontal,
            Theme.language_menu_layout_margin_vertical,
        )
        panel_layout.setSpacing(Theme.menu_option_gap)
        panel_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        panel_layout.addWidget(self._indicator(up=True))

        self._scroll_area = QScrollArea()
        self._scroll_area.setObjectName("LanguageDropdownMenuScrollArea")
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        panel_layout.addWidget(self._scroll_area)

        self._option_list = QWidget()
        self._option_list.setObjectName("LanguageDropdownMenuOptionList")
        self._option_layout = QVBoxLayout(self._option_list)
        self._option_layout.setContentsMargins(0, 0, 0, 0)
        self._option_layout.setSpacing(Theme.menu_option_gap)
        self._option_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._scroll_area.setWidget(self._option_list)

        panel_layout.addWidget(self._indicator(up=False))

    def add_language(
        self,
        language: Language,
        on_selected: Callable[[str], None],
        *,
        selected: bool = False,
    ) -> MenuItem:
        item = MenuItem(
            book_language_display_name(language.iso_code, language.plain_text),
            selected=selected,
        )
        item.clicked.connect(lambda: self._trigger(lambda: on_selected(language.iso_code)))
        self._option_layout.addWidget(item)
        self._item_count += 1
        self.refresh_size()
        return item

    def exec(self, global_pos: QPoint) -> None:
        self.refresh_size()
        self.move(global_pos)
        self.show()
        self.raise_()
        self.activateWindow()
        # Deliberately unparented, and kept in a local for the whole call. A
        # QEventLoop parented to this widget is destroyed with it, and a menu
        # can be destroyed by the very action it triggered. That leaves a
        # dangling pointer in QThreadData::eventLoops, which
        # QCoreApplication::exit() walks calling exit() on every entry -- so
        # the crash lands later, on quit, far from the menu that caused it.
        loop = QEventLoop()
        self._loop = loop
        try:
            loop.exec()
        finally:
            if self._loop is loop:
                self._loop = None
        _refresh_cursor_shape()

    def refresh_size(self) -> None:
        visible_items = min(max(self._item_count, 1), Theme.language_menu_max_visible_items)
        option_height = (visible_items * Theme.menu_item_height) + (
            max(0, visible_items - 1) * Theme.menu_option_gap
        )
        self._option_list.setFixedWidth(
            self._menu_width - (Theme.language_menu_visual_padding_horizontal * 2)
        )
        self._scroll_area.setFixedHeight(option_height)
        self._option_layout.invalidate()
        self._option_list.adjustSize()
        self._panel.adjustSize()
        self.adjustSize()
        panel_height = self._panel.sizeHint().height()
        self._panel.setFixedSize(self._menu_width, panel_height)
        self.setFixedSize(self._menu_width, panel_height)
        self.updateGeometry()

    def _trigger(self, callback: Callable[[], None]) -> None:
        # close() only asks the loop to quit; it returns long before exec()
        # does. Invoking the callback here would run the entire action nested
        # inside this menu's own event loop, so each menu-driven step stacks
        # another loop on the main thread and the menu cannot be torn down
        # while its own exec() is still on the stack. Deferring runs the action
        # after exec() has returned, at the caller's own stack depth.
        self.close()
        QTimer.singleShot(0, callback)

    def _indicator(self, *, up: bool) -> QWidget:
        frame = QWidget()
        frame.setObjectName("LanguageDropdownMenuIndicator")
        frame.setProperty("direction", "up" if up else "down")
        frame.setFixedHeight(Theme.language_menu_indicator_height)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        icon = QLabel()
        icon.setFixedSize(Theme.language_menu_indicator_icon_size, Theme.language_menu_indicator_icon_size)
        pixmap = QIcon(str(self._resources.icon_path("icon_dropout.svg"))).pixmap(
            QSize(Theme.language_menu_indicator_icon_size, Theme.language_menu_indicator_icon_size)
        )
        if up:
            pixmap = pixmap.transformed(QTransform().rotate(180), Qt.TransformationMode.SmoothTransformation)
        icon.setPixmap(pixmap)
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
        return frame

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
    on_export: Callable[[str], None],
    on_remove: Callable[[str], None],
    on_delete: Callable[[str], None],
    *,
    show_remove: bool = True,
    on_hide: Callable[[str], None] | None = None,
    on_unhide: Callable[[str], None] | None = None,
    show_hide_action: bool = False,
) -> FigmaMenu:
    menu = _figma_menu(parent)

    menu.add_item(t("menu.read"), lambda: on_read(book.uuid))
    # Hidden books cannot be favourited (Favourites is one of the
    # surfaces that hidden books vanish from); skip the row entirely so
    # the menu doesn't surface a no-op action.
    if not book.is_hidden:
        menu.add_item(
            t("menu.unfavourite") if book.is_favourite else t("menu.favourite"),
            lambda: on_favourite(book.uuid),
        )
    menu.add_item(t("menu.detail"), lambda: on_detail(book.uuid))
    menu.add_item(t("menu.add_to"), lambda: on_add_to_collection(book.uuid))
    menu.add_item(t("menu.export"), lambda: on_export(book.uuid))
    if show_remove:
        menu.add_item(t("menu.remove"), lambda: on_remove(book.uuid))
    # The Hide/Unhide row is only built when the Privacy toggle is on AND
    # the feature is initialised — both gates are owned by the caller. The
    # menu code itself doesn't reach into settings/state.
    if show_hide_action and on_hide is not None and on_unhide is not None:
        if book.is_hidden:
            menu.add_item(t("menu.unhide"), lambda: on_unhide(book.uuid))
        else:
            menu.add_item(t("menu.hide"), lambda: on_hide(book.uuid))
    menu.add_item(t("menu.delete"), lambda: on_delete(book.uuid), destructive=True)

    return menu


def build_action_menu(
    parent: QWidget,
    on_open_book: Callable[[], None],
    on_open_and_import: Callable[[], None],
    on_import: Callable[[], None],
) -> FigmaMenu:
    menu = _figma_menu(parent)

    menu.add_item(t("menu.open_book"), on_open_book)
    menu.add_item(t("menu.open_and_import"), on_open_and_import)
    menu.add_item(t("menu.import"), on_import)

    return menu


def build_collection_context_menu(
    parent: QWidget,
    collection_uuid: str,
    on_rename: Callable[[str], None],
    on_delete: Callable[[str], None],
    *,
    is_hidable: bool = False,
    on_set_hidable: Callable[[str, bool], None] | None = None,
    show_hide_action: bool = False,
) -> FigmaMenu:
    menu = _figma_menu(parent)

    menu.add_item(t("menu.rename"), lambda: on_rename(collection_uuid))
    if show_hide_action and on_set_hidable is not None:
        if is_hidable:
            menu.add_item(t("menu.make_normal"), lambda: on_set_hidable(collection_uuid, False))
        else:
            menu.add_item(t("menu.make_hidable"), lambda: on_set_hidable(collection_uuid, True))
    menu.add_item(t("menu.delete"), lambda: on_delete(collection_uuid), destructive=True)

    return menu


def build_language_dropdown_menu(
    parent: QWidget,
    resources: ResourceLoader,
    languages: Sequence[Language],
    current_language_tag: str | None,
    on_language_selected: Callable[[str], None],
) -> LanguageDropdownMenu:
    menu = LanguageDropdownMenu(parent, resources)
    for language in languages:
        menu.add_language(
            language,
            on_language_selected,
            selected=language.iso_code == current_language_tag,
        )
    return menu
