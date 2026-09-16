"""Shelf ViewModel's scoped preferences and manual-order commands."""
from dataclasses import replace

from joyread.core.models.shelf_order import ShelfOrder, merge_order, move_group, replace_visible_order
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.signals import Signal


class ShelfSorting:
    """Mixed into ShelfViewModel to keep persistence out of gesture widgets."""

    def _init_sorting(self) -> None:
        self.sort_failed = Signal()
        self.sort_saving = False
        self._sort_scope = self.current_shelf
        self._default_sort = (self.sort_field, self.sort_ascending)
        self._shelf_orders: dict[str, ShelfOrder] = {}
        self._sort_book_snapshot = None
        self._sort_epoch = 0

    def _restore_scope_sort(self) -> None:
        from joyread.ui.viewmodels.shelf_viewmodel import SortField
        self._sort_scope = self.current_shelf
        saved = self._shelf_orders.get(self.current_shelf)
        if saved is None:
            self.sort_field, self.sort_ascending = self._default_sort
        else:
            self.sort_field = SortField(saved.sort_field)
            self.sort_ascending = saved.ascending
        if self.current_shelf == "recent" and self.sort_field == SortField.CUSTOM:
            self.sort_field = SortField.ADD_TIME
        if self.sort_field == SortField.CUSTOM:
            self.sort_ascending = False

    def _scope_books(self, scope: str):
        if scope == "all":
            return self.books  # visibility must never erase hidden books' positions
        if scope == "favourites":
            return [book for book in self.books if book.is_favourite]
        if scope == "hidden":
            return [book for book in self.books if book.is_hidden]
        collection = scope.removeprefix("collection:")
        return [book for book in self.books if collection in book.collection_ids]

    def _prune_sort_memberships(self) -> None:
        if self._sort_book_snapshot is self.books:
            return
        self._sort_book_snapshot = self.books
        for scope, state in tuple(self._shelf_orders.items()):
            members = self._scope_books(scope)
            present = {book.uuid for book in members}
            kept = merge_order(members, state.book_ids) if state.initialized else tuple(key for key in state.book_ids if key in present)
            if kept != state.book_ids:
                self._shelf_orders[scope] = replace(state, book_ids=kept)

    def _manual_ids(self, scope: str | None = None) -> tuple[str, ...]:
        scope = scope or self.current_shelf
        saved = self._shelf_orders.get(scope)
        return merge_order(self._scope_books(scope), saved.book_ids if saved else ())

    @property
    def can_reorder(self) -> bool:
        from joyread.ui.viewmodels.shelf_viewmodel import FileFilter, SortField
        return (self.sort_field == SortField.CUSTOM and self.current_shelf != "recent"
                and not self.search_query.strip() and self.file_filter == FileFilter.ALL
                and not self.tag_filter_active and not self.sort_saving
                and not self.is_loading and not self.is_importing)

    def set_selection(self, selected_ids: set[str]) -> None:
        visible = {book.uuid for book in self.visible_books}
        selected = set(selected_ids) & visible
        if selected != self.selected_book_ids:
            self.selected_book_ids = selected
            self.selection_changed.emit(set(selected))
            self._emit_state()

    def drag_group(self, book_id: str) -> tuple[str, ...]:
        selected = set(self.selected_book_ids)
        if book_id not in selected:
            selected = {book_id}
        return tuple(book.uuid for book in self.visible_books if book.uuid in selected)

    def set_sort(self, field: str, ascending: bool | None = None) -> None:
        from joyread.ui.viewmodels.shelf_viewmodel import SortField
        field = SortField(field)
        if self.sort_saving or (self.current_shelf == "recent" and field == SortField.CUSTOM):
            return
        direction = self.sort_ascending if ascending is None else ascending
        if field == SortField.CUSTOM:
            direction = False
        self.sort_field, self.sort_ascending = field, direction
        if self.current_shelf == "recent":
            self._emit_state()
            return
        previous = self._shelf_orders.get(self.current_shelf)
        initialized = bool(previous and previous.initialized) or field == SortField.CUSTOM
        ids = self._manual_ids() if initialized else ()
        state = ShelfOrder(self.current_shelf, field.value, direction, initialized, ids)
        if state == previous:
            return
        self._save_order(state)

    def reorder_books(self, moving_ids: tuple[str, ...], before_id: str | None) -> None:
        if not self.can_reorder:
            return
        visible = tuple(book.uuid for book in self.visible_books)
        selected = set(moving_ids) & set(visible)
        if not selected:
            return
        reordered = move_group(visible, selected, before_id)
        if reordered == visible:
            return
        state = self._shelf_orders[self.current_shelf]
        self._save_order(replace(state, book_ids=replace_visible_order(self._manual_ids(), reordered)))

    def _save_order(self, state: ShelfOrder) -> None:
        previous = self._shelf_orders.get(state.scope)
        self._shelf_orders[state.scope] = state
        self.sort_saving = True
        epoch = self._sort_epoch
        service = self._library_service
        self._emit_state()

        def write():
            # Restore the actual committed state on failure, not an obsolete UI
            # snapshot: normal library operations can complete during the save.
            try:
                service.save_shelf_order(state)
                return service.list_shelf_orders(), None
            except Exception as error:
                return service.list_shelf_orders(), error

        def done(result):
            if epoch != self._sort_epoch:
                return
            orders, error = result
            self.sort_saving = False
            self._shelf_orders = orders
            self._sort_book_snapshot = None
            self._restore_scope_sort()
            self._emit_state()
            if error is not None:
                self.sort_failed.emit(t("sort.save_failed", detail=str(error)))

        def failed(error):
            if epoch != self._sort_epoch:
                return
            self.sort_saving = False
            if previous is None:
                self._shelf_orders.pop(state.scope, None)
            else:
                self._shelf_orders[state.scope] = previous
            self._restore_scope_sort()
            self._emit_state()
            self.sort_failed.emit(t("sort.save_failed", detail=str(error)))

        if self._task_service is None:
            try:
                done(write())
            except Exception as error:
                failed(error)
        else:
            try:
                self._task_service.submit("save-shelf-order", write, on_success=done, on_failure=failed)
            except Exception as error:
                failed(error)
