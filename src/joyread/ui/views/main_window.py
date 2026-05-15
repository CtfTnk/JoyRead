"""Main JoyRead window."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCloseEvent, QCursor, QIcon
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMainWindow, QSizeGrip, QStackedWidget, QVBoxLayout, QWidget

from joyread.app.app_context import AppContext
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.import_service import BOOK_EXTENSIONS
from joyread.core.models.collection import Collection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.views.reader_shell import ReaderShellWidget
from joyread.ui.views.reader_window import ReaderWindow
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.views.settings_view import SettingsView
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.menus import FigmaMenu, build_collection_context_menu
from joyread.ui.widgets.sidebar import SidebarWidget
from joyread.ui.widgets.window_chrome import WindowChromeWidget


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._reader_windows: list[ReaderWindow] = []
        self._embedded_reader: ReaderShellWidget | None = None
        self.setObjectName("MainWindow")
        self.setWindowTitle("JoyRead")
        self.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(Theme.window_width, Theme.window_height)
        self.setMinimumSize(Theme.window_min_width, Theme.window_min_height)

        root = QWidget()
        root.setObjectName("RootPanel")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.chrome = WindowChromeWidget(context.resources)
        root_layout.addWidget(self.chrome)

        view_panel = QWidget()
        view_panel.setObjectName("ViewPanel")
        layout = QHBoxLayout(view_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.sidebar = SidebarWidget(context.resources)
        self.shelf_view = ShelfView(context.shelf_viewmodel, context.resources)
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("MainContentStack")
        self.content_stack.addWidget(self.shelf_view)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.content_stack, stretch=1)
        root_layout.addWidget(view_panel, stretch=1)

        self.settings_view = SettingsView(context.settings_viewmodel, context.resources, root)
        self.settings_view.close_requested.connect(self._hide_settings_page)
        self.settings_view.hide()

        self._resize_grip = QSizeGrip(root)
        self._resize_grip.setFixedSize(Theme.resize_grip_size, Theme.resize_grip_size)
        self._resize_grip.setObjectName("ResizeGrip")
        self._resize_grip.raise_()

        self.dialog_overlay = JoyReadDialogOverlay(root, context.resources)
        self.dialog_overlay.hide()
        self.setCentralWidget(root)
        self._position_dialog_overlay()

        self.chrome.set_action_menu(self.shelf_view.create_action_menu())
        self.chrome.sidebar_toggle_requested.connect(self._toggle_sidebar)
        self.chrome.view_mode_changed.connect(context.shelf_viewmodel.set_view_mode)
        self.chrome.sort_changed.connect(context.shelf_viewmodel.set_sort)
        self.sidebar.navigation_requested.connect(self._handle_navigation)
        self.sidebar.collection_menu_requested.connect(self._show_collection_menu)
        self.shelf_view.info_requested.connect(self.dialog_overlay.show_info)
        self.shelf_view.import_requested.connect(self._show_import_menu)
        self.shelf_view.delete_books_requested.connect(self._confirm_delete_books)
        self.shelf_view.add_to_collection_requested.connect(self._show_add_to_collection_dialog)
        self.shelf_view.export_books_requested.connect(self._select_export_folder)
        self.shelf_view.read_book_requested.connect(self.open_reader_for_book)
        self.shelf_view.read_book_at_requested.connect(self.open_reader_for_book_at)
        self.shelf_view.open_file_requested.connect(self._select_reader_file)
        self.settings_view.info_requested.connect(self.dialog_overlay.show_info)
        self.settings_view.storage_change_requested.connect(self._select_storage_location)
        context.shelf_viewmodel.state_changed.connect(self._sync_sidebar)
        context.shelf_viewmodel.state_changed.connect(self._sync_chrome)
        context.shelf_viewmodel.books_deleted.connect(self._handle_books_deleted)
        context.shelf_viewmodel.collections_changed.connect(self._handle_collections_changed)
        context.shelf_viewmodel.delete_failed.connect(
            lambda message: self.dialog_overlay.show_info("Delete Failed", message)
        )
        context.shelf_viewmodel.favourite_failed.connect(
            lambda message: self.dialog_overlay.show_info("Favourite Failed", message)
        )
        context.shelf_viewmodel.book_metadata_failed.connect(
            lambda message: self.dialog_overlay.show_info("Book Detail", message)
        )
        context.shelf_viewmodel.collection_failed.connect(
            lambda message: self.dialog_overlay.show_info("Collection", message)
        )
        context.shelf_viewmodel.remove_failed.connect(
            lambda message: self.dialog_overlay.show_info("Remove Failed", message)
        )
        context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(context.shelf_viewmodel.collections)
        self.shelf_view.render()

    def open_reader_for_book(self, book_uuid: str, page_index: int | None = None) -> None:
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        if book is None:
            logger.warning("open_reader_for_book: missing book uuid=%s", book_uuid)
            self.dialog_overlay.show_info("Read", "The selected book is no longer available.")
            return
        individual = self._settings_for_reader_launch().individual_read_window
        logger.info(
            "open_reader_for_book uuid=%s page=%s mode=%s",
            book_uuid,
            page_index,
            "window" if individual else "embedded",
        )
        if individual:
            self._show_reader_window(Path(book.file_path), book=book, start_page_index=page_index)
        else:
            self._show_embedded_reader(Path(book.file_path), book=book, start_page_index=page_index)

    def open_reader_for_book_at(self, book_uuid: str, page_index: int) -> None:
        self.open_reader_for_book(book_uuid, page_index)

    def open_reader_for_file(self, path: str | Path, import_mode: bool = False) -> None:
        source_path = Path(path)
        logger.info("open_reader_for_file path=%s import_mode=%s", source_path, import_mode)
        if not import_mode:
            self._show_reader_window(source_path, title=source_path.stem)
            return

        settings = self._settings_for_reader_launch()
        self._context.task_service.submit(
            "open-and-import-preflight",
            lambda: self._context.import_service.preflight_file(
                source_path,
                archive_internal_max_depth=settings.archive_internal_max_depth,
            ),
            on_success=lambda result, source_path=source_path, settings=settings: self._handle_open_import_preflight(
                source_path,
                settings,
                result,
            ),
            on_failure=lambda error: self.dialog_overlay.show_info("Open & Import Failed", str(error)),
        )

    def _handle_open_import_preflight(self, source_path: Path, settings, result) -> None:  # noqa: ANN001
        if result.can_import:
            self._start_open_and_import(source_path, settings)
            return
        if result.status == "skipped":
            self.dialog_overlay.show_confirm(
                "Open Read Only",
                (
                    "JoyRead won't import this file because it is encrypted or contains encrypted archives.\n\n"
                    "You can still open it for reading."
                ),
                on_confirm=lambda source_path=source_path: self._show_reader_window(source_path, title=source_path.stem),
                confirm_text="Read Only",
                cancel_text="Cancel",
            )
            return
        self.dialog_overlay.show_info("Open & Import Failed", result.message or "This file cannot be imported.")

    def _start_open_and_import(self, source_path: Path, settings) -> None:  # noqa: ANN001
        self._show_reader_window(source_path, title=source_path.stem)
        self._context.task_service.submit(
            "open-and-import-file",
            lambda: self._context.import_service.import_files(
                [source_path],
                archive_internal_max_depth=settings.archive_internal_max_depth,
            ),
            on_success=lambda _result: self._reload_after_background_import(),
            on_failure=lambda error: self.dialog_overlay.show_info("Open & Import Failed", str(error)),
        )

    def _select_reader_file(self, import_mode: bool) -> None:
        extensions = " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_READER_EXTENSIONS))
        # Keep the platform-native picker. On macOS this can briefly involve
        # Open/Save Panel, QuickLook, and AutoFill helper processes owned by
        # the OS; JoyRead does not spawn or manage those helpers directly.
        file_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open Book",
            "",
            f"Readable Books ({extensions})",
        )
        if not file_path:
            return
        self.open_reader_for_file(file_path, import_mode=import_mode)

    def _show_import_menu(self) -> None:
        menu = FigmaMenu(self.shelf_view, width=240)
        menu.add_item("Import Files...", self._select_import_files)
        menu.add_item("Import Folder...", self._select_import_folder)
        menu.add_item("Import JSON Manifest (Dev)...", self._select_import_manifest)
        menu.exec(QCursor.pos())

    def _select_import_files(self) -> None:
        extensions = " ".join(f"*{suffix}" for suffix in sorted(BOOK_EXTENSIONS))
        file_paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Import Books",
            "",
            f"Supported Books ({extensions})",
        )
        if not file_paths:
            return
        settings = self._settings_for_import()
        self._context.task_service.submit(
            "import-files",
            lambda: self._context.import_service.import_files(
                [Path(path) for path in file_paths],
                archive_internal_max_depth=settings.archive_internal_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info("Import Failed", str(error)),
        )

    def _select_import_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose Import Folder",
            str(Path.home()),
        )
        if not directory:
            return
        settings = self._settings_for_import()
        self._context.task_service.submit(
            "import-folder",
            lambda: self._context.import_service.import_folder(
                directory,
                max_depth=settings.import_folder_max_depth,
                archive_internal_max_depth=settings.archive_internal_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info("Import Failed", str(error)),
        )

    def _show_reader_window(
        self,
        path: Path,
        *,
        book=None,
        title: str | None = None,
        start_page_index: int | None = None,
    ) -> None:  # noqa: ANN001
        reader = ReaderWindow(self._context, path, book=book, title=title, start_page_index=start_page_index)
        reader.progress_changed.connect(self._handle_reader_progress_changed)
        reader.closed.connect(lambda reader=reader: self._forget_reader_window(reader))
        reader.destroyed.connect(lambda _obj=None, reader=reader: self._forget_reader_window(reader))
        self._reader_windows.append(reader)
        reader.show()
        reader.raise_()

    def _show_embedded_reader(self, path: Path, *, book, start_page_index: int | None = None) -> None:  # noqa: ANN001
        root = self.centralWidget()
        if root is None:
            return
        self._hide_settings_page()
        self._close_embedded_reader()
        self._embedded_reader = ReaderShellWidget(
            self._context,
            path,
            book=book,
            show_back_button=True,
            start_page_index=start_page_index,
            parent=root,
        )
        self._embedded_reader.back_requested.connect(self._close_embedded_reader)
        self._embedded_reader.progress_changed.connect(self._handle_reader_progress_changed)
        self._embedded_reader.setGeometry(root.rect())
        self._embedded_reader.show()
        self._embedded_reader.raise_()
        self._embedded_reader.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        if hasattr(self, "_resize_grip"):
            self._resize_grip.hide()

    def _close_embedded_reader(self) -> None:
        if self._embedded_reader is None:
            return
        reader = self._embedded_reader
        self._embedded_reader = None
        reader.cancel()
        reader.hide()
        reader.deleteLater()
        if hasattr(self, "_resize_grip"):
            self._resize_grip.show()
            self._resize_grip.raise_()

    def _forget_reader_window(self, reader: ReaderWindow) -> None:
        if reader in self._reader_windows:
            self._reader_windows.remove(reader)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_embedded_reader()
        for reader in tuple(self._reader_windows):
            reader.close()
        super().closeEvent(event)

    def _handle_reader_progress_changed(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        self._context.shelf_viewmodel.apply_reader_progress(book_uuid, page_index, progress_percent)

    def _settings_for_reader_launch(self):
        self._context.settings = self._context.settings_store.load()
        return self._context.settings

    def _settings_for_import(self):
        self._context.settings = self._context.settings_store.load()
        return self._context.settings

    def _reload_after_background_import(self) -> None:
        self._context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(self._context.shelf_viewmodel.collections)
        self.shelf_view.render()

    def _select_import_manifest(self) -> None:
        manifest_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import JoyRead Manifest",
            "",
            "JSON Manifest (*.json)",
        )
        if not manifest_path:
            return
        settings = self._settings_for_import()
        self._context.task_service.submit(
            "import-manifest",
            lambda: self._context.import_service.import_manifest(
                manifest_path,
                archive_internal_max_depth=settings.archive_internal_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info("Import Failed", str(error)),
        )

    def _handle_import_finished(self, result) -> None:  # noqa: ANN001
        self._context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(self._context.shelf_viewmodel.collections)
        self.shelf_view.render()
        self.dialog_overlay.show_info(
            "Import Finished",
            (
                f"Imported: {result.imported_count}\n"
                f"Duplicates: {result.duplicate_count}\n"
                f"Skipped: {getattr(result, 'skipped_count', 0)}\n"
                f"Failed: {result.failed_count}"
            ),
        )

    def _select_export_folder(self, book_uuids: tuple[str, ...]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuids))
        if not target_ids:
            return
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose Export Folder",
            str(Path.home()),
        )
        if not directory:
            return
        self._context.task_service.submit(
            "export-books",
            lambda: self._context.export_service.export_books(target_ids, directory),
            on_success=self._handle_export_finished,
            on_failure=lambda error: self.dialog_overlay.show_info("Export Failed", str(error)),
        )

    def _handle_export_finished(self, result) -> None:  # noqa: ANN001
        lines = [
            f"Exported: {result.exported_count}",
            f"Skipped: {result.skipped_count}",
            f"Failed: {result.failed_count}",
        ]
        failures = [item for item in result.items if item.status == "failed"]
        if failures:
            lines.append("")
            for item in failures[:5]:
                label = item.original_file_name or item.book_uuid
                lines.append(f"{label}: {item.message or 'Export failed.'}")
            if len(failures) > 5:
                lines.append(f"...and {len(failures) - 5} more.")
        self.dialog_overlay.show_info("Export Finished", "\n".join(lines))

    def _confirm_delete_books(self, book_uuids: tuple[str, ...]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuids))
        if not target_ids:
            return
        books_by_uuid = {book.uuid: book for book in self._context.shelf_viewmodel.books}
        titles = [books_by_uuid[book_uuid].title for book_uuid in target_ids if book_uuid in books_by_uuid]
        if not titles:
            return

        if len(titles) == 1:
            title = "Delete Book"
            message = (
                f"Delete '{titles[0]}' from JoyRead?\n\n"
                "This removes its library record, collections, progress, bookmarks, recent history, "
                "and the app-managed copied file."
            )
        else:
            title = "Delete Books"
            message = (
                f"Delete {len(titles)} books from JoyRead?\n\n"
                "This removes their library records, collections, progress, bookmarks, recent history, "
                "and app-managed copied files."
            )
        self.dialog_overlay.show_confirm(
            title,
            message,
            on_confirm=lambda target_ids=target_ids: self._context.shelf_viewmodel.delete_books(target_ids),
            confirm_text="Delete",
            cancel_text="Cancel",
        )

    def _select_storage_location(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose JoyRead Storage Location",
            self._context.settings_viewmodel.storage_location,
        )
        if not directory:
            return
        old_root = Path(self._context.settings.storage_location)
        new_root = Path(directory)
        self._context.database_interpreter.close()
        self._context.task_service.submit(
            "move-storage-location",
            lambda: self._context.storage_migration_service.move_storage_location(old_root, new_root),
            on_success=lambda _result: self._handle_storage_location_changed(),
            on_failure=lambda error: self._handle_storage_location_failed(error),
        )

    def _handle_storage_location_changed(self) -> None:
        self._context.reload_storage_from_settings()
        self._context.shelf_viewmodel.load_books()
        self.sidebar.set_collections(self._context.shelf_viewmodel.collections)
        self.shelf_view.render()
        self.dialog_overlay.show_info("Storage Location", "JoyRead storage location has been updated.")

    def _handle_storage_location_failed(self, error: Exception) -> None:
        self._context.reload_storage_from_settings()
        self.dialog_overlay.show_info("Storage Location", str(error))

    def _handle_navigation(self, key: str) -> None:
        if key == "new_collection":
            self._show_new_collection_dialog()
            return
        if key == "settings":
            self._show_settings_page()
            return
        self._hide_settings_page()
        self._context.shelf_viewmodel.set_current_shelf(key)

    def _show_new_collection_dialog(self) -> None:
        self.dialog_overlay.show_input(
            "New Collection",
            "Collection Name",
            on_confirm=self._context.shelf_viewmodel.create_collection,
            confirm_text="Create",
            cancel_text="Cancel",
            validator=_validate_collection_name,
        )

    def _show_collection_menu(self, collection_key: str, global_pos: QPoint) -> None:
        collection_uuid = _collection_uuid_from_key(collection_key)
        if self._collection_by_uuid(collection_uuid) is None:
            return
        menu = build_collection_context_menu(
            self,
            collection_uuid,
            on_rename=self._show_rename_collection_dialog,
            on_delete=self._confirm_delete_collection,
        )
        menu.exec(global_pos)

    def _show_rename_collection_dialog(self, collection_uuid: str) -> None:
        collection = self._collection_by_uuid(collection_uuid)
        if collection is None:
            return
        self.dialog_overlay.show_input(
            "Rename Collection",
            "Collection Name",
            on_confirm=lambda name, collection_uuid=collection_uuid: self._context.shelf_viewmodel.rename_collection(
                collection_uuid,
                name,
            ),
            initial_text=collection.name,
            confirm_text="Rename",
            cancel_text="Cancel",
            validator=_validate_collection_name,
        )

    def _confirm_delete_collection(self, collection_uuid: str) -> None:
        collection = self._collection_by_uuid(collection_uuid)
        if collection is None:
            return
        self.dialog_overlay.show_confirm(
            "Delete Collection",
            (
                f"Delete '{collection.name}'?\n\n"
                "This removes only the collection and its book membership records. "
                "Books and app-managed files are not deleted."
            ),
            on_confirm=lambda collection_uuid=collection_uuid: self._context.shelf_viewmodel.delete_collection(
                collection_uuid,
            ),
            confirm_text="Delete",
            cancel_text="Cancel",
        )

    def _show_add_to_collection_dialog(self, book_uuids: tuple[str, ...]) -> None:
        collections = list(self._context.shelf_viewmodel.collections)
        if not collections:
            self.dialog_overlay.show_info("Add to Collection", "Create a collection before adding books.")
            return

        def add_to_collection(collection_uuid: str) -> None:
            self._context.shelf_viewmodel.add_books_to_collection(book_uuids, collection_uuid)

        self.dialog_overlay.show_collection_select(
            "Add to Collection",
            collections,
            on_confirm=add_to_collection,
            confirm_text="Add",
            cancel_text="Cancel",
        )

    def _collection_by_uuid(self, collection_uuid: str) -> Collection | None:
        return next(
            (
                collection
                for collection in self._context.shelf_viewmodel.collections
                if collection.uuid == collection_uuid
            ),
            None,
        )

    def _sync_sidebar(self) -> None:
        if self.settings_view.isVisible():
            self.sidebar.set_active("settings")
            return
        current = self._context.shelf_viewmodel.current_shelf
        if current in {ShelfKey.ALL.value, ShelfKey.RECENT.value, ShelfKey.FAVOURITES.value} or current.startswith(
            "collection:"
        ):
            self.sidebar.set_active(current)

    def _sync_chrome(self) -> None:
        self.chrome.set_view_mode(self._context.shelf_viewmodel.view_mode.value)
        self.chrome.set_sort(
            self._context.shelf_viewmodel.sort_field.value,
            self._context.shelf_viewmodel.sort_ascending,
        )

    def _handle_books_deleted(self, _book_uuids: tuple[str, ...]) -> None:
        self.sidebar.set_collections(self._context.shelf_viewmodel.collections)

    def _handle_collections_changed(self, active_key: str | None = None) -> None:
        self.sidebar.set_collections(self._context.shelf_viewmodel.collections)
        self.sidebar.set_active(active_key or self._context.shelf_viewmodel.current_shelf)

    def _toggle_sidebar(self) -> None:
        visible = not self.sidebar.isVisible()
        self.sidebar.setVisible(visible)
        self.shelf_view.set_sidebar_visible(visible)
        self.settings_view.set_sidebar_visible(visible)
        self.chrome.set_sidebar_visible(visible)

    def _show_settings_page(self) -> None:
        self._context.shelf_viewmodel.clear_selection(emit_state=False)
        self._context.shelf_viewmodel.hide_detail()
        self._position_settings_overlay()
        self.settings_view.show()
        self.settings_view.raise_()
        self.settings_view.setFocus(Qt.FocusReason.PopupFocusReason)
        self._raise_dialog_overlay_if_visible()
        self.sidebar.set_active("settings")

    def _hide_settings_page(self) -> None:
        if self.settings_view.isHidden():
            return
        self.settings_view.hide()
        self._sync_sidebar()
        if hasattr(self, "_resize_grip"):
            self._resize_grip.raise_()
        self._raise_dialog_overlay_if_visible()

    def _position_settings_overlay(self) -> None:
        if not hasattr(self, "settings_view"):
            return
        root = self.centralWidget()
        if root is None:
            return
        self.settings_view.setGeometry(0, 0, root.width(), root.height())

    def _position_dialog_overlay(self) -> None:
        if not hasattr(self, "dialog_overlay"):
            return
        root = self.centralWidget()
        if root is None:
            return
        self.dialog_overlay.setGeometry(0, 0, root.width(), root.height())
        self._raise_dialog_overlay_if_visible()

    def _raise_dialog_overlay_if_visible(self) -> None:
        if hasattr(self, "dialog_overlay") and self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_settings_overlay()
        self._position_dialog_overlay()
        if self._embedded_reader is not None and self.centralWidget() is not None:
            self._embedded_reader.setGeometry(self.centralWidget().rect())
            self._embedded_reader.raise_()
        if hasattr(self, "_resize_grip"):
            margin = 2
            self._resize_grip.move(
                self.centralWidget().width() - self._resize_grip.width() - margin,
                self.centralWidget().height() - self._resize_grip.height() - margin,
            )


def _validate_collection_name(name: str) -> str | None:
    return None if name.strip() else "Collection name cannot be empty."


def _collection_uuid_from_key(collection_key: str) -> str:
    prefix = "collection:"
    return collection_key[len(prefix) :] if collection_key.startswith(prefix) else collection_key
