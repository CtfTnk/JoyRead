"""Mouse-session controller shared by the grid and list bookshelf surfaces.

Idle filters watch only surface presses. The application filter and mouse grab
exist only during a gesture, so external file drops and other windows keep their
normal event paths. Domain selection/order changes are commands on the VM.
"""
from time import monotonic
from weakref import WeakSet

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from joyread.ui.resources.styles.theme import Theme
from joyread.ui.widgets.book_grid import BookGridWidget
from joyread.ui.widgets.shelf_drag_visuals import DragCardPreview, DropPlaceholder, SelectionRectangle, card_snapshots


class ShelfGestureController(QObject):
    def __init__(self, owner, viewmodel, surfaces, resources):
        super().__init__(owner)
        self._owner = owner
        self._vm = viewmodel
        self._surfaces = surfaces
        self._resources = resources
        self._watched = WeakSet()
        self._surface = None
        self._mode = None
        self._suppressed = False
        self._releasing = False
        self._preview = None
        self._placeholder = None
        self._rubber = None
        self._index = 0
        self._moving = ()
        self._wheel_until = 0.0
        self._scroll_fraction = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(Theme.shelf_drag_scroll_interval_ms)
        self._timer.timeout.connect(self._auto_scroll)
        for surface in surfaces:
            self._watch(surface.viewport())
            self._watch(surface._content)
            surface.verticalScrollBar().valueChanged.connect(self._scrolled)

    @property
    def active(self):
        return self._mode is not None

    def _watch(self, widget):
        if widget not in self._watched:
            widget.installEventFilter(self)
            self._watched.add(widget)

    def bind_controls(self):
        for surface in self._surfaces:
            for control in surface.book_controls.values():
                self._watch(control)

    def _current_signature(self):
        vm = self._vm
        return (vm.current_shelf, vm.view_mode, vm.sort_field, vm.sort_ascending,
                vm.search_query, vm.file_filter, vm.tag_filter_ids,
                tuple(book.uuid for book in vm.visible_books), vm.is_loading, vm.is_importing)

    def synchronize(self):
        if self.active and (self._signature != self._current_signature()
                            or (self._mode == "drag" and not self._vm.can_reorder)):
            self.cancel(restore_selection=False, suppress_release=True)

    def eventFilter(self, watched, event):
        kind = event.type()
        if self._suppressed:
            if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Close, QEvent.Type.Hide) and watched is self._owner.window():
                self._suppressed = False
                self._release_tracking()
                return False
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._suppressed = False
                self._release_tracking()
                return True
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.ContextMenu):
                return True
        if self.active:
            if kind == QEvent.Type.ShortcutOverride and event.key() == Qt.Key.Key_Escape:
                event.accept()
                return True
            if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.cancel(suppress_release=True)
                return True
            if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Close, QEvent.Type.Hide) and watched is self._owner.window():
                self.cancel()
                return False
            if kind == QEvent.Type.UngrabMouse and watched is self._surface.viewport() and not self._releasing:
                self.cancel()
                return False
            if kind == QEvent.Type.ContextMenu:
                return True
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
                self.cancel(suppress_release=True)
                return True
            if kind == QEvent.Type.MouseMove:
                if self._mode == "pressed":
                    self._additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.move(event.globalPosition().toPoint())
                return True
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self.release(event.globalPosition().toPoint())
                return True
            if kind == QEvent.Type.Wheel:
                self._last_global = event.globalPosition().toPoint()
                if self._inside():
                    bar = self._surface.verticalScrollBar()
                    delta = event.pixelDelta().y() or event.angleDelta().y() / 120 * bar.singleStep() * 3
                    self._wheel_until = monotonic() + Theme.shelf_drag_wheel_pause_ms / 1000
                    bar.setValue(bar.value() - round(delta))
                    self._update_visuals()
                return True
            return False
        if kind != QEvent.Type.MouseButtonPress or not isinstance(event, QMouseEvent):
            return False
        if event.button() != Qt.MouseButton.LeftButton or watched not in self._watched:
            return False
        surface = next((view for view in self._surfaces if watched in (view.viewport(), view._content)
                        or watched in view.book_controls.values()), None)
        if surface is None or not surface.isVisible():
            return False
        key = watched.book.uuid if hasattr(watched, "book") else None
        self.begin(surface, key, event.globalPosition().toPoint(), bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        return True

    def begin(self, surface, key, global_pos, additive=False):
        self._surface = surface
        self._mode = "pressed"
        self._pressed_key = key
        self._additive = additive
        self._selection_before = set(self._vm.selected_book_ids)
        self._signature = self._current_signature()
        self._original_ids = self._signature[7]
        self._press_global = QPoint(global_pos)
        self._last_global = QPoint(global_pos)
        self._press_content = surface._content.mapFromGlobal(global_pos)
        self._moving = ()
        self._wheel_until = 0.0
        self._scroll_fraction = 0.0
        QApplication.instance().installEventFilter(self)
        surface.viewport().setFocus(Qt.FocusReason.MouseFocusReason)
        surface.viewport().grabMouse()

    def move(self, global_pos):
        if not self.active:
            return
        self._last_global = QPoint(global_pos)
        if self._mode == "pressed" and (global_pos - self._press_global).manhattanLength() >= QApplication.startDragDistance():
            # Shift reserves dragging for additive rectangle selection, even
            # when the gesture starts on an already-selected book.
            if self._pressed_key is None or self._additive:
                self._mode = "rubber"
                self._rubber = SelectionRectangle(self._surface._content)
                self._rubber.show()
            elif self._vm.can_reorder:
                self._start_drag()
        self._update_visuals()

    def _start_drag(self):
        self._moving = self._vm.drag_group(self._pressed_key)
        if not self._moving:
            return
        self._mode = "drag"
        self._vm.set_selection(set(self._moving))
        control = self._surface.book_controls[self._moving[0]]
        snapshots = card_snapshots(self._surface, self._moving, self._resources)
        self._preview = DragCardPreview(snapshots, len(self._moving), self._owner.window())
        self._placeholder = DropPlaceholder(control.grab(), control.size(), self._surface._content)
        self._remaining = tuple(key for key in self._original_ids if key not in set(self._moving))
        first = self._original_ids.index(self._moving[0])
        self._index = sum(key not in self._moving for key in self._original_ids[:first])
        self._surface.show_reorder_preview(self._moving, self._index, self._placeholder)
        self._placeholder.fade_in()
        self._preview.fade_in()
        self._last_tick = monotonic()
        self._timer.start()

    def _inside(self):
        viewport = self._surface.viewport()
        return viewport.rect().contains(viewport.mapFromGlobal(self._last_global))

    def _update_visuals(self):
        if self._mode == "rubber":
            point = self._surface._content.mapFromGlobal(self._last_global)
            rect = QRect(self._press_content, point).normalized()
            self._rubber.setGeometry(rect)
            self._rubber.raise_()
            hit = {key for key, widget in self._surface.book_controls.items()
                   if widget.geometry().intersected(rect).width() > 0 and widget.geometry().intersected(rect).height() > 0}
            self._vm.set_selection(hit | self._selection_before if self._additive else hit)
        elif self._mode == "drag":
            window = self._owner.window()
            pos = window.mapFromGlobal(self._last_global)
            self._preview.setVisible(window.rect().contains(pos))
            self._preview.move(pos + QPoint(Theme.shelf_drag_offset, Theme.shelf_drag_offset)
                               - self._preview.card_origin)
            self._preview.raise_()
            self._surface.viewport().setCursor(Qt.CursorShape.ClosedHandCursor if self._inside() else Qt.CursorShape.ForbiddenCursor)
            if self._inside():
                point = self._surface._content.mapFromGlobal(self._last_global)
                index = self._insertion_index(point)
                if index != self._index:
                    self._index = index
                    self._surface.show_reorder_preview(self._moving, index, self._placeholder)

    def _insertion_index(self, point):
        if self._placeholder.geometry().contains(point):
            return self._index
        widgets = [self._surface.book_controls[key] for key in self._remaining]
        if not widgets:
            return 0
        if not isinstance(self._surface, BookGridWidget):
            return next((i for i, widget in enumerate(widgets) if point.y() < widget.geometry().center().y()), len(widgets))
        if point.y() < min(widget.y() for widget in widgets):
            return 0
        if point.y() > max(widget.geometry().bottom() for widget in widgets):
            return len(widgets)
        row_y = min(widgets, key=lambda widget: abs(widget.geometry().center().y() - point.y())).y()
        row = [(i, widget) for i, widget in enumerate(widgets) if widget.y() == row_y]
        return next((i for i, widget in row if point.x() < widget.geometry().center().x()), row[-1][0] + 1)

    def _scrolled(self, value):
        if self.active:
            self._update_visuals()

    def _auto_scroll(self):
        now = monotonic()
        elapsed = min(0.1, now - self._last_tick)
        self._last_tick = now
        if self._mode != "drag" or not self._inside() or now < self._wheel_until:
            return
        viewport = self._surface.viewport()
        y = viewport.mapFromGlobal(self._last_global).y()
        bottom = viewport.rect().bottom()
        edge = max(1, viewport.height() * Theme.shelf_drag_scroll_edge_ratio)
        if y < edge:
            fraction = -(edge - y) / edge
        elif y > bottom - edge:
            fraction = (y - (bottom - edge)) / edge
        else:
            self._scroll_fraction = 0.0
            return
        self._scroll_fraction += fraction * Theme.shelf_drag_scroll_max_speed * elapsed
        pixels = int(self._scroll_fraction)
        self._scroll_fraction -= pixels
        if pixels:
            bar = self._surface.verticalScrollBar()
            bar.setValue(bar.value() + pixels)

    def release(self, global_pos):
        if not self.active:
            return
        self._last_global = QPoint(global_pos)
        mode, key, additive = self._mode, self._pressed_key, self._additive
        if not self._inside():
            self.cancel()
            return
        self._update_visuals()
        moving = self._moving
        before = self._remaining[self._index] if mode == "drag" and self._index < len(self._remaining) else None
        self._finish()
        if mode == "drag":
            self._vm.reorder_books(moving, before)
        elif mode == "pressed":
            if key is not None:
                self._surface.book_selected.emit(key, additive)
            elif not additive:
                self._surface.blank_clicked.emit()

    def cancel(self, *, restore_selection=True, suppress_release=False):
        if not self.active:
            return
        selected = self._selection_before
        self._finish(suppress_release=suppress_release)
        if restore_selection:
            self._vm.set_selection(selected)

    def _finish(self, *, suppress_release=False):
        self._mode = None  # restoring selection can synchronously render the shelf
        self._timer.stop()
        if self._rubber is not None:
            self._rubber.hide()
            self._rubber.deleteLater()
            self._rubber = None
        if self._placeholder is not None:
            rect = self._placeholder.geometry()
            self._surface.clear_reorder_preview()
            self._placeholder.setGeometry(rect)
            self._placeholder.fade_out()
            self._placeholder = None
        if self._preview is not None:
            self._preview.fade_out()
            self._preview = None
        self._surface.viewport().unsetCursor()
        self._suppressed = suppress_release
        if not suppress_release:
            self._release_tracking()

    def _release_tracking(self):
        self._releasing = True
        if self._surface is not None:
            self._surface.viewport().releaseMouse()
        QApplication.instance().removeEventFilter(self)
        self._releasing = False
