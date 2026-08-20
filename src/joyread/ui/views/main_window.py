"""Main JoyRead window."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal as QtSignal
from PySide6.QtGui import QCloseEvent, QCursor, QIcon
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QMainWindow, QSizeGrip, QStackedWidget, QVBoxLayout, QWidget

from joyread.app.app_context import AppContext, StorageTransition
from joyread.app.cover_editor import PreparedCoverSource
from joyread.app.storage_transition import TransitionConsequences, describe_consequences
from joyread.app.storage_transition_driver import StorageTransitionController
from joyread.app.windows.novel_provider import EmbeddedReaderShell, NovelReaderProvider
from joyread.app.windows.requests import StandaloneReaderLauncher, StandaloneReaderRequest
from joyread.core.file_types import EPUB_EXTENSIONS
from joyread.core.models.book import Book
from joyread.infrastructure.i18n.locale_service import t
from joyread.infrastructure.logging import log_event
from joyread.core.models.tag import Tag
from joyread.core.reader import SUPPORTED_READER_EXTENSIONS
from joyread.core.services.import_service import BOOK_EXTENSIONS
from joyread.core.services.library_maintenance_service import LibraryAuditPlan, LibraryAuditReport
from joyread.app.tasking import TaskPriority
from joyread.core.models.collection import Collection
from joyread.ui.resources.styles.theme import Theme
from joyread.ui.viewmodels.cover_editor_viewmodel import CoverEditorThumbnailViewModel
from joyread.ui.views.reader_shell import ReaderShellWidget
from joyread.ui.viewmodels.shelf_viewmodel import ShelfKey
from joyread.ui.views.settings_view import SettingsView
from joyread.ui.views.shelf_view import ShelfView
from joyread.ui.widgets.cover_editor import CoverEditorOverlay
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.hidden_space_lock import HiddenSpaceLockOverlay
from joyread.ui.widgets.menus import FigmaMenu, build_collection_context_menu
from joyread.ui.widgets.sidebar import SidebarWidget
from joyread.ui.widgets.window_chrome import TitleBarWidget


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    closed = QtSignal()

    def __init__(
        self,
        context: AppContext,
        *,
        standalone_reader_launcher: StandaloneReaderLauncher | None = None,
        storage_transition_controller: StorageTransitionController | None = None,
        novel_reader_provider: NovelReaderProvider | None = None,
    ) -> None:
        super().__init__()
        self._context = context
        self._standalone_reader_launcher = standalone_reader_launcher
        # Absent unless the composition root wired the novel reader in, which
        # is what makes an .epub unopenable rather than a special case here.
        self._novel_reader_provider = novel_reader_provider
        # Supplied by the window manager, which owns the Reader windows a
        # transition has to flush and close. Without one there are no Readers
        # to account for, which is exactly the situation in focused tests.
        self._storage_transition = storage_transition_controller or StorageTransitionController(
            context, None
        )
        self._storage_transition.finished.connect(self._complete_storage_transition)
        self._storage_transition.failed.connect(self._handle_storage_location_failed)
        self._storage_transition.abandoned.connect(self._handle_storage_transition_abandoned)
        # Either reader shell, named by what Main actually requires of one
        # rather than by a class -- the novel shell is not importable here.
        self._embedded_reader: EmbeddedReaderShell | None = None
        self._cover_editor_book_uuid: str | None = None
        self._library_maintenance_task_active = False
        self._library_maintenance_plan_pending = False
        if context.thumbnail_renderer is None:
            raise RuntimeError("AppContext must provide a cover preview renderer")
        self._cover_editor_thumbnail_viewmodel = CoverEditorThumbnailViewModel(
            context.thumbnail_service,
            context.task_service,
            context.thumbnail_renderer,
            context.archive_warmup_coordinator,
        )
        context.settings_viewmodel.archive_open_limits_changed.connect(
            self._invalidate_archive_thumbnail_sources
        )
        self.setObjectName("MainWindow")
        self.setWindowTitle("JoyRead")
        self.setWindowIcon(QIcon(str(context.resources.app_icon_path())))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(Theme.window_width, Theme.window_height)
        self.setMinimumSize(Theme.window_min_width, Theme.window_min_height)

        root = QWidget()
        root.setObjectName("RootPanel")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.title_bar = TitleBarWidget(context.resources)
        self.chrome = self.title_bar
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

        self.settings_view = SettingsView(
            context.settings_viewmodel,
            context.resources,
            root,
            tag_viewmodel=context.tag_management_viewmodel,
        )
        self.settings_view.close_requested.connect(self._hide_settings_page)
        self.settings_view.tag_operation_completed.connect(self._handle_tag_operation_result)
        self.settings_view.tag_delete_requested.connect(self._confirm_delete_tags)
        self.settings_view.library_maintenance_requested.connect(self._request_library_maintenance)
        self.settings_view.hide()

        self._resize_grip = QSizeGrip(root)
        self._resize_grip.setFixedSize(Theme.resize_grip_size, Theme.resize_grip_size)
        self._resize_grip.setObjectName("ResizeGrip")
        self._resize_grip.raise_()

        self.cover_editor_overlay = CoverEditorOverlay(context.resources, root)
        self.cover_editor_overlay.hide()
        self.dialog_overlay = JoyReadDialogOverlay(root, context.resources)
        self.dialog_overlay.hide()
        self.setCentralWidget(root)
        self._position_cover_editor_overlay()
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
        self.shelf_view.open_file_requested.connect(self._select_reader_file)
        self.shelf_view.tag_filter_requested.connect(self._show_tag_filter_dialog)
        self.shelf_view.detail_tag_filter_requested.connect(self._activate_detail_tag_filter)
        self.shelf_view.detail_tag_allocation_requested.connect(self._show_book_tag_allocation_dialog)
        self.shelf_view.cover_edit_requested.connect(self._show_cover_editor)
        self.cover_editor_overlay.import_requested.connect(self._select_cover_editor_image)
        self.cover_editor_overlay.thumbnail_interest_changed.connect(
            self._cover_editor_thumbnail_viewmodel.set_interest
        )
        self.cover_editor_overlay.thumbnail_interest_released.connect(
            self._cover_editor_thumbnail_viewmodel.release_interest
        )
        self.cover_editor_overlay.picker_visibility_changed.connect(
            self._handle_cover_picker_visibility_changed
        )
        self.cover_editor_overlay.thumbnail_selected.connect(self._load_cover_editor_thumbnail_source)
        self.cover_editor_overlay.save_requested.connect(self._confirm_cover_editor_save)
        self.cover_editor_overlay.closed.connect(self._clear_cover_editor_book_uuid)
        self._cover_editor_thumbnail_viewmodel.source_ready.connect(
            self._handle_cover_editor_thumbnail_source_ready
        )
        self._cover_editor_thumbnail_viewmodel.thumbnail_ready.connect(
            self.cover_editor_overlay.set_thumbnail
        )
        self._cover_editor_thumbnail_viewmodel.failed.connect(self._handle_cover_editor_source_failed)
        self._cover_editor_thumbnail_viewmodel.preview_ready.connect(
            self._handle_cover_editor_source_loaded
        )
        self._cover_editor_thumbnail_viewmodel.preview_failed.connect(
            self._handle_cover_editor_preview_failed
        )
        self._cover_editor_thumbnail_viewmodel.cover_saved.connect(
            self._handle_cover_editor_cover_saved
        )
        self._cover_editor_thumbnail_viewmodel.save_failed.connect(
            self._handle_cover_editor_save_failed
        )
        self.settings_view.info_requested.connect(self.dialog_overlay.show_info)
        self.settings_view.storage_move_requested.connect(self._request_move_storage)
        self.settings_view.storage_select_requested.connect(self._request_select_storage)
        self.settings_view.storage_reset_requested.connect(self._request_reset_storage)
        self.settings_view.hidden_space_setup_requested.connect(self._show_hidden_space_setup_dialog)
        self.settings_view.hidden_space_verify_requested.connect(self._show_hidden_space_unlock_dialog)
        self.settings_view.hidden_space_change_password_requested.connect(
            self._show_hidden_space_change_password_dialog
        )
        self.settings_view.hidden_space_revert_requested.connect(self._show_hidden_space_revert_dialog)
        self.settings_view.hidden_space_reset_requested.connect(self._show_hidden_space_reset_dialog)
        context.shelf_viewmodel.state_changed.connect(self._sync_sidebar)
        context.shelf_viewmodel.state_changed.connect(self._sync_chrome)
        context.shelf_viewmodel.books_deleted.connect(self._handle_books_deleted)
        context.shelf_viewmodel.collections_changed.connect(self._handle_collections_changed)
        # Hidden Space toggles live on the SettingsViewModel; mirror them
        # into the ShelfViewModel and re-render the sidebar so the Hidden
        # row + hidable collections appear/disappear in lockstep.
        context.settings_viewmodel.hidden_space_changed.connect(self._handle_hidden_space_changed)
        context.settings_viewmodel.hidden_space_error.connect(self._show_hidden_space_error)
        context.settings_viewmodel.language_changed.connect(self._on_language_changed)
        context.settings_viewmodel.state_changed.connect(self._sync_title_control_mode)
        # Shelf clicks travel through the viewmodel (book_card →
        # shelf_view → vm.open_book) so every "open" is gated by
        # ``_refresh_book_state`` — that re-validates the
        # ``is_available`` snapshot per click and heals a stale missing row
        # (e.g. user restored a deleted file). MainWindow only sees
        # the VM's *decision* signals and never reads
        # ``book.is_available`` directly.
        context.shelf_viewmodel.book_open_requested.connect(self.open_reader_for_book)
        context.shelf_viewmodel.book_open_at_requested.connect(self.open_reader_for_book_at)
        context.shelf_viewmodel.missing_book_requested.connect(self._show_missing_book_dialog)
        context.shelf_viewmodel.unavailable_book_requested.connect(self._show_unavailable_book_dialog)
        context.shelf_viewmodel.delete_failed.connect(self._show_delete_failed)
        context.shelf_viewmodel.favourite_failed.connect(self._show_favourite_failed)
        context.shelf_viewmodel.book_metadata_failed.connect(self._show_book_metadata_failed)
        context.shelf_viewmodel.book_cover_updated.connect(self._handle_book_cover_updated)
        context.shelf_viewmodel.book_cover_failed.connect(self._show_book_cover_failed)
        context.shelf_viewmodel.book_tags_failed.connect(self._show_book_tags_failed)
        context.shelf_viewmodel.collection_failed.connect(self._show_collection_failed)
        context.shelf_viewmodel.remove_failed.connect(self._show_remove_failed)
        context.shelf_viewmodel.load_books()
        self._refresh_sidebar_collections()
        self.shelf_view.render()
        self._sync_title_control_mode()

        # Launch-time Hidden Space gate. If the user closed the previous
        # session with "Show Collections" still on, the shelf is hidden
        # behind an #ECECEC lock overlay until they verify the password
        # (or press Hide to flip the toggle off). Read both flags from
        # the ViewModel so the View layer doesn't reach into Service /
        # settings directly.
        self._lock_overlay: HiddenSpaceLockOverlay | None = None
        settings_vm = context.settings_viewmodel
        if settings_vm.show_hidden_collection and settings_vm.hidden_space_initialized:
            self._show_hidden_space_lock_overlay(root)

        # Surface a recovery notice once if startup had to fall back to another
        # library (configured storage missing, schema unsupported, etc.).
        if context.storage_startup_notice:
            QTimer.singleShot(
                0,
                lambda message=context.storage_startup_notice: self.dialog_overlay.show_info(
                    t("dialog.storage_title"), message
                ),
            )
        if context.library_maintenance_recovery_conflicts:
            QTimer.singleShot(
                0,
                lambda: self.dialog_overlay.show_info(
                    t("dialog.library_maintenance_recovery_title"),
                    t("dialog.library_maintenance_recovery_msg"),
                ),
            )

    def open_reader_for_book(self, book_uuid: str, page_index: int | None = None) -> None:
        # Invoked via ``shelf_viewmodel.book_open_requested`` — the VM
        # has already refreshed file state and gated on ``is_available``,
        # so we only defend against the race where the book row was
        # deleted between the VM check and this slot.
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        if book is None:
            logger.warning("open_reader_for_book: missing book uuid=%s", book_uuid)
            self.dialog_overlay.show_info(t("dialog.read_title"), t("dialog.book_no_longer_available"))
            return
        if not book.is_available:
            self._show_unavailable_book_dialog(book_uuid)
            return
        source_path = Path(book.file_path)
        if self._is_shelved_epub(source_path):
            self._show_epub_unavailable()
            return
        individual = self._settings_for_reader_launch().individual_read_window
        is_novel = self._is_novel_source(source_path)
        logger.info(
            "open_reader_for_book uuid=%s page=%s mode=%s reader=%s",
            book_uuid,
            page_index,
            "window" if individual else "embedded",
            "novel" if is_novel else "manga",
        )
        # Both reader kinds route the same way from here: the window manager
        # picks the window class from the provider, and the embedded path
        # picks the shell the same way, so there is no novel branch to take.
        if individual:
            self._show_reader_window(source_path, book=book, start_page_index=page_index)
        else:
            self._show_embedded_reader(source_path, book=book, start_page_index=page_index)

    def open_reader_for_book_at(self, book_uuid: str, page_index: int) -> None:
        self.open_reader_for_book(book_uuid, page_index)

    def activate_tag_filter(self, tags: Iterable[Tag], target_shelf: str | None = None) -> None:
        self._hide_settings_page()
        if target_shelf is not None:
            self._context.shelf_viewmodel.set_current_shelf(target_shelf)
        tag_ids = tuple(dict.fromkeys(tag.tag_id for tag in tags if tag.tag_id))
        self._context.shelf_viewmodel.set_tag_filter_ids(tag_ids)
        self.shelf_view.render()

    def open_reader_for_file(self, path: str | Path, import_mode: bool = False) -> None:
        source_path = Path(path)
        logger.info("open_reader_for_file path=%s import_mode=%s", source_path, import_mode)
        if self._is_shelved_epub(source_path):
            self._show_epub_unavailable()
            return
        if self._is_novel_source(source_path):
            # Read-only even when the novel reader is wired in; importing one
            # needs its own validation path first.
            self._show_reader_window(source_path, title=source_path.stem)
            return
        if not import_mode:
            self._show_reader_window(source_path, title=source_path.stem)
            return

        # Reader access is independent from managed-library import. Open the
        # source immediately; the background job validates only the staged
        # managed copy and a failure must never close this reader window.
        settings = self._settings_for_reader_launch()
        self._start_open_and_import(source_path, settings)

    def _is_novel_source(self, path: Path) -> bool:
        """Whether a wired-in novel reader claims this path."""

        provider = self._novel_reader_provider
        return provider is not None and path.suffix.lower() in provider.extensions

    def _is_shelved_epub(self, path: Path) -> bool:
        """An EPUB on the shelf that nothing present can open.

        Only reachable from a library built while the novel reader was
        available -- imports reject ``.epub`` while it is not -- so this exists
        to explain the file rather than to fail silently on it.
        """

        return path.suffix.lower() in EPUB_EXTENSIONS and not self._is_novel_source(path)

    def _show_epub_unavailable(self) -> None:
        self.dialog_overlay.show_info(
            t("dialog.read_title"),
            t("dialog.epub_temporarily_unavailable"),
        )

    def _start_open_and_import(self, source_path: Path, settings) -> None:  # noqa: ANN001
        logger.info("Open & Import starting path=%s", source_path)
        self._show_reader_window(source_path, title=source_path.stem)
        self._context.task_service.submit(
            "open-and-import-file",
            lambda: self._context.import_service.import_files(
                [source_path],
                nested_archive_max_depth=settings.nested_archive_max_depth,
                archive_global_file_max_depth=settings.archive_global_file_max_depth,
            ),
            on_success=self._handle_open_and_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info(t("dialog.open_import_failed_title"), str(error)),
        )

    def _select_reader_file(self, import_mode: bool) -> None:
        readable_suffixes = sorted(SUPPORTED_READER_EXTENSIONS)
        extensions = " ".join(f"*{suffix}" for suffix in readable_suffixes)
        # Keep the platform-native picker. On macOS this can briefly involve
        # Open/Save Panel, QuickLook, and AutoFill helper processes owned by
        # the OS; JoyRead does not spawn or manage those helpers directly.
        file_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            t("dialog.file_open_book_title"),
            "",
            t("dialog.file_filter_readable_books", extensions=extensions),
        )
        if not file_path:
            return
        self.open_reader_for_file(file_path, import_mode=import_mode)

    def _show_import_menu(self) -> None:
        menu = FigmaMenu(self.shelf_view, width=240)
        menu.add_item(t("menu.import_files"), self._select_import_files)
        menu.add_item(t("menu.import_folder"), self._select_import_folder)
        menu.add_item(t("menu.import_json"), self._select_import_manifest)
        menu.exec(QCursor.pos())

    def _select_import_files(self) -> None:
        extensions = " ".join(f"*{suffix}" for suffix in sorted(BOOK_EXTENSIONS))
        file_paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            t("dialog.file_import_books_title"),
            "",
            t("dialog.file_filter_supported_books", extensions=extensions),
        )
        if not file_paths:
            return
        settings = self._settings_for_import()
        logger.info("Import files selected count=%d", len(file_paths))
        self._context.task_service.submit(
            "import-files",
            lambda: self._context.import_service.import_files(
                [Path(path) for path in file_paths],
                nested_archive_max_depth=settings.nested_archive_max_depth,
                archive_global_file_max_depth=settings.archive_global_file_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info(t("dialog.import_failed_title"), str(error)),
        )

    def _select_import_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            t("dialog.file_choose_import_folder_title"),
            str(Path.home()),
        )
        if not directory:
            return
        settings = self._settings_for_import()
        logger.info("Import folder selected path=%s depth=%d", directory, settings.import_folder_max_depth)
        self._context.task_service.submit(
            "import-folder",
            lambda: self._context.import_service.import_folder(
                directory,
                max_depth=settings.import_folder_max_depth,
                nested_archive_max_depth=settings.nested_archive_max_depth,
                archive_global_file_max_depth=settings.archive_global_file_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info(t("dialog.import_failed_title"), str(error)),
        )

    def _show_reader_window(
        self,
        path: Path,
        *,
        book=None,
        title: str | None = None,
        start_page_index: int | None = None,
    ) -> None:  # noqa: ANN001
        self._launch_standalone_reader(
            StandaloneReaderRequest(
                path=path,
                book=book,
                title=title,
                start_page_index=start_page_index,
            )
        )

    def _show_embedded_reader(self, path: Path, *, book, start_page_index: int | None = None) -> None:  # noqa: ANN001
        root = self.centralWidget()
        if root is None:
            return
        self._hide_settings_page()
        self._close_embedded_reader()
        provider = self._novel_reader_provider
        if provider is not None and self._is_novel_source(path):
            self._embedded_reader = provider.create_embedded_shell(
                self._context,
                path,
                book=book,
                show_back_button=True,
                start_page_index=start_page_index,
                parent=root,
            )
        else:
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
        self.setMinimumSize(Theme.reader_min_width, Theme.reader_min_height)
        if hasattr(self, "_resize_grip"):
            self._resize_grip.hide()

    def _close_embedded_reader(self) -> None:
        if self._embedded_reader is None:
            return
        logger.debug("Closing embedded reader")
        reader = self._embedded_reader
        self._embedded_reader = None
        reader.cancel()
        reader.hide()
        reader.deleteLater()
        self.setMinimumSize(Theme.window_min_width, Theme.window_min_height)
        if hasattr(self, "_resize_grip"):
            self._resize_grip.show()
            self._resize_grip.raise_()

    def _launch_standalone_reader(self, request: StandaloneReaderRequest) -> None:
        launcher = self._standalone_reader_launcher
        if launcher is None:
            raise RuntimeError("MainWindow requires a standalone reader launcher for window mode.")
        logger.debug(
            "Requesting standalone reader path=%s book=%s start_page=%s",
            request.path,
            getattr(request.book, "uuid", None),
            request.start_page_index,
        )
        launcher(request)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_embedded_reader()
        self._cover_editor_thumbnail_viewmodel.cancel()
        self.closed.emit()
        super().closeEvent(event)

    def _invalidate_archive_thumbnail_sources(self) -> None:
        """Refresh the active cover picker after a new limits snapshot."""

        self._cover_editor_thumbnail_viewmodel.invalidate_source()

    def _handle_reader_progress_changed(self, book_uuid: str, page_index: int, progress_percent: float) -> None:
        logger.debug(
            "Reader progress callback book=%s page=%d percent=%.2f",
            book_uuid,
            page_index,
            progress_percent,
        )
        self._context.shelf_viewmodel.apply_reader_progress(book_uuid, page_index, progress_percent)

    def _show_hidden_space_error(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.hidden_space_title"), message)

    def _show_delete_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.delete_failed_title"), message)

    def _show_favourite_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.favourite_failed_title"), message)

    def _show_book_metadata_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.book_detail_title"), message)

    def _show_book_cover_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.cover_editor_title"), message)

    def _show_book_tags_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.book_tags_title"), message)

    def _show_collection_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.collection_title"), message)

    def _show_remove_failed(self, message: str) -> None:
        self.dialog_overlay.show_info(t("dialog.remove_failed_title"), message)

    def _settings_for_reader_launch(self):
        return self._context.reload_settings()

    def _settings_for_import(self):
        return self._context.reload_settings()

    def _reload_after_background_import(self) -> None:
        logger.debug("Reloading shelf after background import")
        self._context.shelf_viewmodel.load_books()
        self._refresh_sidebar_collections()
        self.shelf_view.render()

    def _handle_open_and_import_finished(self, result) -> None:  # noqa: ANN001
        """Refresh the shelf without coupling Reader lifetime to import result."""

        self._reload_after_background_import()
        problem = next(
            (item for item in result.items if item.status not in {"imported", "duplicate"}),
            None,
        )
        if problem is not None:
            self.dialog_overlay.show_info(
                t("dialog.open_import_failed_title"),
                problem.message or t("dialog.open_import_unsupported"),
            )

    def _select_import_manifest(self) -> None:
        manifest_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            t("dialog.file_import_manifest_title"),
            "",
            t("dialog.file_filter_json_manifest"),
        )
        if not manifest_path:
            return
        settings = self._settings_for_import()
        logger.info("Import manifest selected path=%s", manifest_path)
        self._context.task_service.submit(
            "import-manifest",
            lambda: self._context.import_service.import_manifest(
                manifest_path,
                nested_archive_max_depth=settings.nested_archive_max_depth,
                archive_global_file_max_depth=settings.archive_global_file_max_depth,
            ),
            on_success=self._handle_import_finished,
            on_failure=lambda error: self.dialog_overlay.show_info(t("dialog.import_failed_title"), str(error)),
        )

    def _handle_import_finished(self, result) -> None:  # noqa: ANN001
        logger.info(
            "Import UI callback finished imported=%d duplicate=%d skipped=%d failed=%d",
            result.imported_count,
            result.duplicate_count,
            getattr(result, "skipped_count", 0),
            result.failed_count,
        )
        self._context.shelf_viewmodel.load_books()
        self._refresh_sidebar_collections()
        self.shelf_view.render()
        self.dialog_overlay.show_info(
            t("dialog.import_finished_title"),
            t(
                "dialog.import_finished_msg",
                imported=str(result.imported_count),
                duplicates=str(result.duplicate_count),
                skipped=str(getattr(result, "skipped_count", 0)),
                failed=str(result.failed_count),
            ),
        )

    def _request_library_maintenance(self) -> None:
        if self._library_maintenance_task_active or self._library_maintenance_plan_pending:
            return
        self._library_maintenance_task_active = True
        self._submit_library_maintenance_task(
            "library-maintenance-scan",
            self._context.library_maintenance_service.scan,
            on_success=self._handle_library_maintenance_scan,
            on_failure=self._handle_library_maintenance_failure,
            priority=TaskPriority.BACKGROUND,
        )

    def _handle_library_maintenance_scan(self, plan: LibraryAuditPlan) -> None:
        self._library_maintenance_task_active = False
        if not plan.has_changes:
            self.dialog_overlay.show_info(
                t("dialog.library_audit_title"),
                t("dialog.library_audit_clean_msg"),
            )
            return
        self._library_maintenance_plan_pending = True
        self.dialog_overlay.show_confirm(
            t("dialog.library_audit_title"),
            t(
                "dialog.library_audit_plan_msg",
                changed=str(plan.changed_count),
                merged=str(plan.merge_count),
                missing=str(plan.missing_count),
                unavailable=str(plan.unavailable_count),
                repaired=str(plan.repair_count),
                orphan_files=str(len(plan.orphan_files)),
                orphan_cache=str(len(plan.orphan_cache_files)),
                reclaimable=_format_byte_count(plan.reclaimable_bytes),
            ),
            on_confirm=lambda plan=plan: self._apply_library_maintenance(plan),
            on_cancel=self._clear_pending_library_maintenance_plan,
            confirm_text=t("dialog.btn_apply"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _apply_library_maintenance(self, plan: LibraryAuditPlan) -> None:
        self._library_maintenance_plan_pending = False
        if self._library_maintenance_task_active:
            return
        self._library_maintenance_task_active = True
        self._submit_library_maintenance_task(
            "library-maintenance-apply",
            lambda plan=plan: self._context.library_maintenance_service.apply(plan),
            on_success=self._handle_library_maintenance_report,
            on_failure=self._handle_library_maintenance_failure,
            priority=TaskPriority.NORMAL,
        )

    def _handle_library_maintenance_report(self, report: LibraryAuditReport) -> None:
        self._library_maintenance_task_active = False
        self._context.shelf_viewmodel.load_books()
        self._refresh_sidebar_collections()
        self.shelf_view.render()
        self._context.settings_viewmodel.refresh_archive_pool_usage()
        self.dialog_overlay.show_info(
            t("dialog.library_audit_title"),
            t(
                "dialog.library_audit_report_msg",
                changed=str(report.changed_count),
                merged=str(report.merged_count),
                missing=str(report.missing_count),
                unavailable=str(report.unavailable_count),
                repaired=str(report.repaired_count),
                cleaned_files=str(report.cleaned_file_count),
                cleaned_cache=str(report.cleaned_cache_count),
                reclaimed=_format_byte_count(report.reclaimed_bytes),
                skipped=str(len(report.skipped)),
                errors=str(len(report.errors)),
            ),
        )

    def _handle_library_maintenance_failure(self, error: Exception) -> None:
        self._library_maintenance_task_active = False
        self._library_maintenance_plan_pending = False
        logger.warning("Library maintenance task failed: %s", error)
        self.dialog_overlay.show_info(
            t("dialog.library_audit_title"),
            t("dialog.library_audit_failed_msg", message=str(error)),
        )

    def _clear_pending_library_maintenance_plan(self) -> None:
        self._library_maintenance_plan_pending = False

    def _submit_library_maintenance_task(self, name, callback, *, on_success, on_failure, priority) -> None:  # noqa: ANN001
        """Use priority when the injected test task service supports it."""

        try:
            self._context.task_service.submit(
                name,
                callback,
                on_success=on_success,
                on_failure=on_failure,
                priority=priority,
            )
        except TypeError:
            self._context.task_service.submit(
                name,
                callback,
                on_success=on_success,
                on_failure=on_failure,
            )

    def _select_export_folder(self, book_uuids: tuple[str, ...]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuids))
        if not target_ids:
            return
        directory = QFileDialog.getExistingDirectory(
            self,
            t("dialog.file_choose_export_folder_title"),
            str(Path.home()),
        )
        if not directory:
            return
        logger.info("Export folder selected count=%d directory=%s", len(target_ids), directory)
        self._context.task_service.submit(
            "export-books",
            lambda: self._context.export_service.export_books(target_ids, directory),
            on_success=self._handle_export_finished,
            on_failure=lambda error: self.dialog_overlay.show_info(t("dialog.export_failed_title"), str(error)),
        )

    def _handle_export_finished(self, result) -> None:  # noqa: ANN001
        logger.info(
            "Export UI callback finished exported=%d skipped=%d failed=%d",
            result.exported_count,
            result.skipped_count,
            result.failed_count,
        )
        lines = [
            t("dialog.export_summary_exported", count=str(result.exported_count)),
            t("dialog.export_summary_skipped", count=str(result.skipped_count)),
            t("dialog.export_summary_failed", count=str(result.failed_count)),
        ]
        failures = [item for item in result.items if item.status == "failed"]
        if failures:
            lines.append("")
            for item in failures[:5]:
                label = item.original_file_name or item.book_uuid
                lines.append(f"{label}: {item.message or t('dialog.export_item_failed')}")
            if len(failures) > 5:
                lines.append(t("dialog.export_more_failures", count=str(len(failures) - 5)))
        self.dialog_overlay.show_info(t("dialog.export_finished_title"), "\n".join(lines))

    def _confirm_delete_books(self, book_uuids: tuple[str, ...]) -> None:
        target_ids = tuple(dict.fromkeys(book_uuids))
        if not target_ids:
            return
        books_by_uuid = {book.uuid: book for book in self._context.shelf_viewmodel.books}
        titles = [books_by_uuid[book_uuid].title for book_uuid in target_ids if book_uuid in books_by_uuid]
        if not titles:
            return

        if len(titles) == 1:
            title = t("dialog.delete_book_title")
            message = t("dialog.delete_book_msg", title=titles[0])
        else:
            title = t("dialog.delete_books_title")
            message = t("dialog.delete_books_msg", count=str(len(titles)))
        self.dialog_overlay.show_confirm(
            title,
            message,
            on_confirm=lambda target_ids=target_ids: self._context.shelf_viewmodel.delete_books(target_ids),
            confirm_text=t("dialog.btn_delete"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _show_missing_book_dialog(self, book_uuid: str) -> None:
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        if book is None:
            self.dialog_overlay.show_info(t("dialog.missing_file_title"), t("dialog.missing_file_not_found"))
            return
        # Destructive action on Confirm, dismiss on Cancel — matches
        # ``_confirm_delete_books`` so Esc / click-outside don't
        # silently delete the book. Unicode quotes survive titles
        # that contain an apostrophe.
        book_title = book.title or "(untitled)"
        self.dialog_overlay.show_confirm(
            t("dialog.missing_file_title"),
            t("dialog.missing_file_msg", title=book_title),
            on_confirm=lambda book_uuid=book_uuid: self._context.shelf_viewmodel.delete_books((book_uuid,)),
            on_cancel=None,
            confirm_text=t("dialog.btn_delete"),
            cancel_text=t("dialog.btn_keep"),
        )

    def _show_unavailable_book_dialog(self, book_uuid: str) -> None:
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        title = book.title if book is not None else t("dialog.book_no_longer_available")
        self.dialog_overlay.show_info(
            t("dialog.unavailable_file_title"),
            t("dialog.unavailable_file_msg", title=title),
        )

    def _show_hidden_space_setup_dialog(self) -> None:
        # First-time enable of "Show Collections": prompt for new password +
        # confirmation + a plaintext hint. The dialog stays open on
        # validation failure (mismatched / weak password) via the
        # state-prompt; final confirm only fires when the VM accepts.
        from PySide6.QtWidgets import QLineEdit

        echo_modes = (QLineEdit.EchoMode.Password, QLineEdit.EchoMode.Password, QLineEdit.EchoMode.Normal)

        def on_confirm(values: tuple[str, ...]) -> None:
            password, confirm, hint = (values + ("", "", ""))[:3]
            ok = self._context.settings_viewmodel.initialize_hidden_space(password, confirm, hint or None)
            if not ok:
                # Validation error already surfaced via hidden_space_error;
                # the page re-renders so the switch snaps back off.
                self.settings_view.page.revert_show_hidden_switch()

        def on_cancel() -> None:
            self.settings_view.page.revert_show_hidden_switch()

        def validator(values: tuple[str, ...]) -> str | None:
            # Cheap pre-flight before the VM call so the most common
            # mistakes don't write a state-prompt by emitting an error
            # signal that may bypass the dialog.
            password, confirm = (values + ("", "", ""))[:2]
            if len(password) < 4 or not password.isalnum() or not password.isascii():
                return t("dialog.password_rules_error")
            if password != confirm:
                return t("dialog.password_mismatch")
            return None

        self.dialog_overlay.show_multi_password_input(
            t("dialog.set_password_title"),
            (t("dialog.password_header"), t("dialog.confirm_password_header"), t("dialog.hint_header")),
            on_confirm=on_confirm,
            echo_modes=echo_modes,
            confirm_text=t("dialog.btn_confirm"),
            cancel_text=t("dialog.btn_cancel"),
            validator=validator,
            on_cancel=on_cancel,
        )

    def _show_hidden_space_unlock_dialog(self) -> None:
        # Toggling Show Collections on once the feature is initialised:
        # single password input. The hint is shown as detail text so the
        # user can recover from a memory lapse without escaping the dialog.
        hint = self._context.settings_viewmodel.hidden_space_hint
        detail = t("dialog.hint_prefix", hint=hint) if hint else None

        def on_confirm(password: str) -> None:
            if self._context.settings_viewmodel.verify_hidden_space_password(password):
                self._context.settings_viewmodel.set_show_hidden_collection(True)
                return
            self.settings_view.page.revert_show_hidden_switch()
            self.dialog_overlay.show_info(t("dialog.hidden_space_title"), t("dialog.incorrect_password"))

        def on_cancel() -> None:
            self.settings_view.page.revert_show_hidden_switch()

        self.dialog_overlay.show_password_input(
            t("dialog.unlock_title"),
            t("dialog.password_header"),
            on_confirm=on_confirm,
            confirm_text=t("dialog.btn_verify"),
            cancel_text=t("dialog.btn_cancel"),
            on_cancel=on_cancel,
            detail_text=detail,
        )

    def _show_hidden_space_change_password_dialog(self) -> None:
        from PySide6.QtWidgets import QLineEdit

        echo_modes = (
            QLineEdit.EchoMode.Password,
            QLineEdit.EchoMode.Password,
            QLineEdit.EchoMode.Password,
        )

        def validator(values: tuple[str, ...]) -> str | None:
            _old, new, confirm = (values + ("", "", ""))[:3]
            if len(new) < 4 or not new.isalnum() or not new.isascii():
                return t("dialog.new_password_rules_error")
            if new != confirm:
                return t("dialog.new_password_mismatch")
            return None

        def on_confirm(values: tuple[str, ...]) -> None:
            old, new, confirm = (values + ("", "", ""))[:3]
            ok = self._context.settings_viewmodel.change_hidden_space_password(old, new, confirm)
            if ok:
                self.dialog_overlay.show_info(t("dialog.hidden_space_title"), t("dialog.password_updated"))

        self.dialog_overlay.show_multi_password_input(
            t("dialog.change_password_title"),
            (t("dialog.current_password_header"), t("dialog.new_password_header"), t("dialog.confirm_new_password_header")),
            on_confirm=on_confirm,
            echo_modes=echo_modes,
            confirm_text=t("dialog.btn_update"),
            cancel_text=t("dialog.btn_cancel"),
            validator=validator,
        )

    def _show_hidden_space_revert_dialog(self) -> None:
        # "Revert all" is password-gated per the user spec: verifying the
        # password is itself the confirmation, so no second confirm dialog.
        def on_confirm(password: str) -> None:
            if not self._context.settings_viewmodel.verify_hidden_space_password(password):
                self.dialog_overlay.show_info(t("dialog.hidden_space_title"), t("dialog.incorrect_password"))
                return
            self._context.settings_viewmodel.revert_hidden_space()
            self.dialog_overlay.show_info(
                t("dialog.hidden_space_title"),
                t("dialog.hidden_reverted_msg"),
            )

        self.dialog_overlay.show_password_input(
            t("dialog.revert_hidden_title"),
            t("dialog.password_header"),
            on_confirm=on_confirm,
            confirm_text=t("dialog.btn_revert"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _show_hidden_space_reset_dialog(self) -> None:
        # "Reset and Erase" is intentionally not password-gated — the user
        # asked for an escape hatch in case the password is forgotten. We
        # still gate it behind a destructive-styled confirmation so it
        # can't fire by accident.
        self.dialog_overlay.show_confirm(
            t("dialog.reset_hidden_title"),
            t("dialog.reset_hidden_msg"),
            on_confirm=self._context.settings_viewmodel.reset_hidden_space,
            on_cancel=None,
            confirm_text=t("dialog.btn_erase"),
            cancel_text=t("dialog.btn_cancel"),
            destructive=True,
        )

    def _handle_tag_operation_result(self, success: bool, title: str, message: str) -> None:
        # Both success and failure flow through ``show_info`` so the user
        # always sees an explicit outcome for tag CRUD.
        if success:
            # The tag dialogs read the ViewModel's cached list, so a tag just
            # created or renamed here has to reach that cache or it would not
            # appear until the next full shelf reload.
            self._context.shelf_viewmodel.refresh_tags_async()
        self.dialog_overlay.show_info(title, message)

    def _confirm_delete_tags(self, title: str, message: str) -> None:
        self.dialog_overlay.show_confirm(
            title,
            message,
            on_confirm=self._context.tag_management_viewmodel.delete_selected,
            confirm_text=t("dialog.btn_delete"),
            cancel_text=t("dialog.btn_cancel"),
            destructive=True,
        )

    def _request_move_storage(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            t("dialog.file_choose_library_parent_title"),
            self._context.settings_viewmodel.storage_location,
        )
        if not directory:
            return
        target_parent = Path(directory)
        self._confirm_storage_transition(
            lambda: self._context.begin_storage_move(target_parent)
        )

    def _request_select_storage(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            t("dialog.file_select_existing_library_title"),
            self._context.settings_viewmodel.storage_location,
        )
        if not directory:
            return
        existing_root = Path(directory)
        # Validation and adoption are one exclusive transition. Splitting them
        # would let an import slip in after validation but before the selected
        # storage root is adopted.
        self._confirm_storage_transition(
            lambda: self._context.begin_storage_select(existing_root)
        )

    def _confirm_storage_transition(self, operation: Callable[[], StorageTransition]) -> None:
        """Get explicit consent, then quiesce the application and migrate.

        Replacing the storage root replaces the services every Reader session
        is bound to, and a live session cannot be re-pointed at a rebuilt
        stack. So the consequences are stated up front rather than discovered:
        open Readers close, and an in-progress cover edit is discarded.
        """

        consequences = describe_consequences(
            len(self._open_reader_windows()),
            self.cover_editor_overlay.isVisible(),
        )
        if not consequences.closes_anything:
            self._begin_storage_transition(operation)
            return
        self.dialog_overlay.show_confirm(
            t("dialog.storage_title"),
            self._storage_transition_warning(consequences),
            on_confirm=lambda: self._begin_storage_transition(operation),
            confirm_text=t("dialog.btn_continue"),
            cancel_text=t("dialog.btn_cancel"),
        )

    @staticmethod
    def _storage_transition_warning(consequences: TransitionConsequences) -> str:
        count = str(consequences.reader_windows)
        if consequences.reader_windows and consequences.discards_cover_edit:
            return t("dialog.storage_quiesce_readers_and_cover", count=count)
        if consequences.reader_windows:
            return t("dialog.storage_quiesce_readers", count=count)
        return t("dialog.storage_quiesce_cover")

    def _open_reader_windows(self) -> tuple[object, ...]:
        manager = self._storage_transition.window_manager
        return tuple(getattr(manager, "reader_windows", ()))

    def _begin_storage_transition(self, operation: Callable[[], StorageTransition]) -> None:
        # Confirmed, so the cover edit goes now: the user was told it would.
        if self.cover_editor_overlay.isVisible():
            self.cover_editor_overlay.hide()
            self._clear_cover_editor_book_uuid()
        if not self._storage_transition.start(operation):
            self.dialog_overlay.show_info(
                t("dialog.storage_title"), t("dialog.storage_transition_busy")
            )

    def _handle_storage_transition_abandoned(self, pending_tasks: int) -> None:
        """Quiescence was not reached, so storage was never touched."""

        logger.warning(
            "Storage transition abandoned with %d task(s) still running", pending_tasks
        )
        self.dialog_overlay.show_info(
            t("dialog.storage_title"), t("dialog.storage_quiesce_timeout")
        )

    def _request_reset_storage(self) -> None:
        self.dialog_overlay.show_confirm(
            t("dialog.reset_library_title"),
            t("dialog.reset_library_msg"),
            on_confirm=self._prompt_reset_storage_confirmation,
            confirm_text=t("dialog.btn_continue"),
            cancel_text=t("dialog.btn_cancel"),
            destructive=True,
        )

    def _prompt_reset_storage_confirmation(self) -> None:
        self.dialog_overlay.show_input(
            t("dialog.reset_library_title"),
            t("dialog.type_delete_header"),
            on_confirm=lambda _value: self._execute_reset_storage(),
            confirm_text=t("dialog.btn_delete"),
            cancel_text=t("dialog.btn_cancel"),
            validator=lambda value: None if value.strip().lower() == "delete" else t("dialog.type_delete_error"),
        )

    def _execute_reset_storage(self) -> None:
        # Reset closes Readers and discards a cover edit exactly as Move does,
        # and its own two dialogs only ever mention erasing data. Sharing the
        # consequence gate says so; it shows nothing when nothing is open, so
        # the common case is still two dialogs rather than three.
        self._confirm_storage_transition(self._context.begin_storage_reset)

    def _complete_storage_transition(self, transition: StorageTransition) -> None:
        """Finish a worker-held storage mutation on the Qt/UI thread."""

        try:
            self._context.finish_storage_transition(transition)
        except Exception as error:
            logger.warning("Storage transition reload failed: %s", error, exc_info=True)
            self.dialog_overlay.show_info(t("dialog.storage_title"), str(error))
            return
        finally:
            # Background work stays sealed until the services around it have
            # been rebuilt, including when the rebuild itself failed -- a
            # half-rebuilt stack is the one state nothing else may run against.
            self._context.resume_after_storage_transition()
            self._storage_transition.acknowledge()
        if transition.error is not None:
            log_event(
                logger,
                logging.WARNING,
                "storage.transition.result_failed",
                "Storage transition returned a controlled failure",
                category="storage",
                status="failed",
                error_type=type(transition.error).__name__,
                reason=str(transition.error),
            )
            self.dialog_overlay.show_info(t("dialog.storage_title"), str(transition.error))
            return
        result = transition.result
        if result is not None and not getattr(result, "ok", True):
            message = getattr(result, "message", "") or t("dialog.storage_invalid_default")
            log_event(
                logger,
                logging.WARNING,
                "storage.transition.result_rejected",
                "Storage transition result was rejected",
                category="storage",
                status="failed",
                reason=message,
            )
            self.dialog_overlay.show_info(
                t("dialog.storage_title"),
                t("dialog.storage_invalid_location", message=message),
            )
            return
        self._handle_storage_location_changed(reload=False)

    def _handle_storage_location_changed(self, *, reload: bool = True) -> None:
        if reload:
            self._context.reload_storage_from_settings()
        self._context.shelf_viewmodel.load_books()
        self._refresh_sidebar_collections()
        self.shelf_view.render()
        self.dialog_overlay.show_info(t("dialog.storage_title"), t("dialog.storage_updated_msg"))

    def _handle_storage_location_failed(self, error: Exception) -> None:
        # The disk phase raised past the commit point, so the services closed
        # for it have to be rebuilt before anything is allowed to run again.
        rebuild_error: Exception | None = None
        try:
            self._context.reload_storage_from_settings()
        except Exception as caught:
            rebuild_error = caught
            log_event(
                logger,
                logging.ERROR,
                "storage.transition.rebuild_failed",
                "Storage services failed to rebuild after a transition error",
                category="storage",
                status="failed",
                error_type=type(caught).__name__,
                reason=str(caught),
                exc_info=True,
            )
        finally:
            self._context.storage_rebuild_required = False
            self._context.resume_after_storage_transition()
            self._storage_transition.acknowledge()
        if rebuild_error is not None:
            self.dialog_overlay.show_info(t("dialog.storage_title"), str(rebuild_error))
            return
        log_event(
            logger,
            logging.WARNING,
            "storage.transition.failure_presented",
            "Storage transition failure was presented to the user",
            category="storage",
            status="failed",
            error_type=type(error).__name__,
            reason=str(error),
        )
        self.dialog_overlay.show_info(t("dialog.storage_title"), str(error))

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
            t("dialog.new_collection_title"),
            t("dialog.collection_name_header"),
            on_confirm=self._context.shelf_viewmodel.create_collection,
            confirm_text=t("dialog.btn_create"),
            cancel_text=t("dialog.btn_cancel"),
            validator=_validate_collection_name,
        )

    def _show_collection_menu(self, collection_key: str, global_pos: QPoint) -> None:
        collection_uuid = _collection_uuid_from_key(collection_key)
        collection = self._collection_by_uuid(collection_uuid)
        if collection is None:
            return
        shelf_vm = self._context.shelf_viewmodel
        show_hide_action = shelf_vm.hidden_space_initialized and shelf_vm.show_hidden_collection
        menu = build_collection_context_menu(
            self,
            collection_uuid,
            on_rename=self._show_rename_collection_dialog,
            on_delete=self._confirm_delete_collection,
            is_hidable=collection.is_hidable,
            on_set_hidable=shelf_vm.set_collection_hidable,
            show_hide_action=show_hide_action,
        )
        menu.exec(global_pos)

    def _show_rename_collection_dialog(self, collection_uuid: str) -> None:
        collection = self._collection_by_uuid(collection_uuid)
        if collection is None:
            return
        self.dialog_overlay.show_input(
            t("dialog.rename_collection_title"),
            t("dialog.collection_name_header"),
            on_confirm=lambda name, collection_uuid=collection_uuid: self._context.shelf_viewmodel.rename_collection(
                collection_uuid,
                name,
            ),
            initial_text=collection.name,
            confirm_text=t("dialog.btn_rename"),
            cancel_text=t("dialog.btn_cancel"),
            validator=_validate_collection_name,
        )

    def _confirm_delete_collection(self, collection_uuid: str) -> None:
        collection = self._collection_by_uuid(collection_uuid)
        if collection is None:
            return
        self.dialog_overlay.show_confirm(
            t("dialog.delete_collection_title"),
            t("dialog.delete_collection_msg", name=collection.name),
            on_confirm=lambda collection_uuid=collection_uuid: self._context.shelf_viewmodel.delete_collection(
                collection_uuid,
            ),
            confirm_text=t("dialog.btn_delete"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _show_add_to_collection_dialog(self, book_uuids: tuple[str, ...]) -> None:
        # ``visible_collections`` already filters hidable rows out when
        # the Privacy toggle is off, so the Add-to dialog can't expose
        # hidable targets while the feature is dormant.
        collections = list(self._context.shelf_viewmodel.visible_collections)
        if not collections:
            self.dialog_overlay.show_info(t("dialog.add_to_collection_title"), t("dialog.no_collection_msg"))
            return

        def add_to_collection(collection_uuid: str) -> None:
            self._context.shelf_viewmodel.add_books_to_collection(book_uuids, collection_uuid)

        self.dialog_overlay.show_collection_select(
            t("dialog.add_to_collection_title"),
            collections,
            on_confirm=add_to_collection,
            confirm_text=t("dialog.btn_add"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _show_tag_filter_dialog(self) -> None:
        # Read the ViewModel's cached tags rather than querying here. The
        # repository call blocks on the database actor's queue, so opening this
        # dialog while an import or audit is running froze the window until
        # that work drained. The ViewModel keeps the list current off-thread.
        tags = list(self._context.shelf_viewmodel.available_tags)

        def apply_filter(tag_ids: tuple[str, ...]) -> None:
            try:
                self._context.shelf_viewmodel.set_tag_filter_ids(tag_ids)
            except Exception as exc:  # pragma: no cover - defensive UI boundary.
                logger.exception("Applying tag filter failed: %s", exc)
                self.dialog_overlay.show_info(t("dialog.tag_filter_title"), t("dialog.tag_filter_apply_failed"))

        self.dialog_overlay.show_tag_filter(
            t("dialog.tag_filter_title"),
            tags,
            self._context.shelf_viewmodel.tag_filter_ids,
            apply_filter,
        )

    def _activate_detail_tag_filter(self, book_uuid: str, tag_id: str) -> None:
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        tag = next((tag for tag in self._context.shelf_viewmodel.available_tags if tag.tag_id == tag_id), None)
        if book is None or tag is None:
            logger.warning("Detail tag filter request ignored book=%s tag=%s", book_uuid, tag_id)
            return
        target_shelf = ShelfKey.HIDDEN.value if book.is_hidden else ShelfKey.ALL.value
        self._context.shelf_viewmodel.hide_detail()
        self.activate_tag_filter((tag,), target_shelf=target_shelf)

    def _show_book_tag_allocation_dialog(self, book_uuid: str) -> None:
        book = next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)
        if book is None:
            self.dialog_overlay.show_info(t("dialog.book_tags_title"), t("dialog.book_no_longer_available"))
            return
        # Cached for the same reason as the tag filter above.
        tags = list(self._context.shelf_viewmodel.available_tags)

        def assign_tags(tag_ids: tuple[str, ...]) -> None:
            self._context.shelf_viewmodel.set_book_tag_ids(book_uuid, tag_ids)

        self.dialog_overlay.show_tag_allocation(
            t("dialog.assign_tags_title"),
            tags,
            self._context.shelf_viewmodel.tag_ids_for_book(book_uuid),
            assign_tags,
        )

    def _show_cover_editor(self, book_uuid: str) -> None:
        self._cover_editor_thumbnail_viewmodel.replace_thumbnail_service(
            self._context.thumbnail_service
        )
        book = self._book_by_uuid(book_uuid)
        if book is None:
            self.dialog_overlay.show_info(t("dialog.cover_editor_title"), t("dialog.book_no_longer_available"))
            return
        if not self._context.thumbnail_service.can_generate_from(book):
            logger.info("Cover editor unavailable for book=%s path=%s", book_uuid, book.file_path)
            self.dialog_overlay.show_info(
                t("dialog.cover_editor_title"),
                t("dialog.cover_editor_unavailable"),
            )
            return

        self._cover_editor_book_uuid = book_uuid
        self._cover_editor_thumbnail_viewmodel.set_book(
            book,
            (Theme.detail_thumbnail_width, Theme.detail_thumbnail_height),
        )
        self._cover_editor_thumbnail_viewmodel.load_page_source(
            0,
            self._cover_editor_preview_size(),
            opening=True,
        )

    def _select_cover_editor_image(self) -> None:
        file_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            t("dialog.file_import_cover_image_title"),
            "",
            t("dialog.file_filter_images"),
        )
        if not file_path:
            return
        self._cover_editor_thumbnail_viewmodel.load_import_source(
            Path(file_path),
            self._cover_editor_preview_size(),
        )

    def _load_cover_editor_thumbnail_source(self, page_index: int) -> None:
        if self._cover_editor_book() is None:
            return
        self._cover_editor_thumbnail_viewmodel.load_page_source(
            page_index,
            self._cover_editor_preview_size(),
            opening=False,
        )

    def _confirm_cover_editor_save(self, crop_state) -> None:  # noqa: ANN001
        book_uuid = self._cover_editor_book_uuid
        if book_uuid is None:
            return
        self.dialog_overlay.show_confirm(
            t("dialog.replace_cover_title"),
            t("dialog.replace_cover_msg"),
            on_confirm=lambda book_uuid=book_uuid, crop_state=crop_state: (
                self._save_cover_editor_cover(book_uuid, crop_state)
            ),
            confirm_text=t("dialog.btn_confirm"),
            cancel_text=t("dialog.btn_cancel"),
        )

    def _save_cover_editor_cover(self, book_uuid: str, crop_state) -> None:  # noqa: ANN001
        book = self._book_by_uuid(book_uuid)
        if book is None:
            self.dialog_overlay.show_info(t("dialog.cover_editor_title"), t("dialog.book_no_longer_available"))
            return
        self._cover_editor_thumbnail_viewmodel.save_cover(
            crop_state,
            (Theme.cover_width, Theme.cover_height),
        )

    def _handle_cover_editor_source_loaded(
        self,
        book_uuid: str,
        source: PreparedCoverSource[object],
        opening: bool,
    ) -> None:
        if self._cover_editor_book_uuid != book_uuid:
            logger.debug(
                "Cover editor source dropped book=%s active_book=%s source=%s",
                book_uuid,
                self._cover_editor_book_uuid,
                source.source_token,
            )
            return

        updated = (
            self.cover_editor_overlay.open_editor(source.frame, source.source_token)
            if opening
            else self.cover_editor_overlay.set_source(source.frame, source.source_token)
        )
        if not updated:
            logger.warning("Cover editor rejected prepared frame book=%s", book_uuid)
            self.dialog_overlay.show_info(
                t("dialog.cover_editor_title"),
                t("dialog.cover_editor_load_source_image_failed"),
            )
            if opening:
                self._clear_cover_editor_book_uuid()
            return
        self._position_cover_editor_overlay()
        logger.debug(
            "Cover editor source loaded book=%s source=%s dimensions=%s opening=%s",
            book_uuid,
            source.source_token,
            source.source_dimensions,
            opening,
        )

    def _handle_cover_editor_thumbnail_source_ready(self, book_uuid: str, page_count: int) -> None:
        if self._cover_editor_book_uuid == book_uuid:
            self.cover_editor_overlay.set_thumbnail_page_count(page_count)

    def _handle_cover_picker_visibility_changed(self, visible: bool) -> None:
        detail_book_uuid = self._context.shelf_viewmodel.detail_book_uuid
        if detail_book_uuid is None:
            return
        if visible:
            self._context.shelf_viewmodel.release_detail_thumbnail_interest(detail_book_uuid)
            return
        self._context.shelf_viewmodel.refresh_detail_thumbnail_interest(detail_book_uuid)
        self.shelf_view.detail_panel.refresh_thumbnail_interest()

    def _handle_cover_editor_source_failed(self, error: Exception) -> None:
        logger.warning(
            "Cover editor thumbnail source open failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self.dialog_overlay.show_info(t("dialog.cover_editor_title"), t("dialog.cover_editor_load_source_failed"))

    def _handle_cover_editor_preview_failed(self, error: Exception, opening: bool) -> None:
        logger.warning(
            "Cover editor preview preparation failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self.dialog_overlay.show_info(
            t("dialog.cover_editor_title"),
            t("dialog.cover_editor_load_source_image_failed"),
        )
        if opening:
            self._clear_cover_editor_book_uuid()

    def _handle_cover_editor_save_failed(self, error: Exception) -> None:
        logger.warning(
            "Cover editor save failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self.dialog_overlay.show_info(t("dialog.cover_editor_title"), t("dialog.cover_editor_save_failed"))

    def _handle_cover_editor_cover_saved(self, book_uuid: str, path: Path) -> None:
        if self._cover_editor_book_uuid != book_uuid:
            return
        self._context.shelf_viewmodel.set_book_cover_path(book_uuid, path)

    def _handle_book_cover_updated(self, book_uuid: str, _path: Path) -> None:
        if self._cover_editor_book_uuid == book_uuid:
            self.cover_editor_overlay.hide()
            self._cover_editor_book_uuid = None

    def _clear_cover_editor_book_uuid(self) -> None:
        if not self.cover_editor_overlay.isVisible():
            self._cover_editor_thumbnail_viewmodel.cancel()
            self._cover_editor_book_uuid = None

    def _book_by_uuid(self, book_uuid: str) -> Book | None:
        return next((book for book in self._context.shelf_viewmodel.books if book.uuid == book_uuid), None)

    def _cover_editor_book(self) -> Book | None:
        if self._cover_editor_book_uuid is None:
            return None
        return self._book_by_uuid(self._cover_editor_book_uuid)

    def _cover_editor_preview_size(self) -> tuple[int, int]:
        dpr = max(1.0, float(self.devicePixelRatioF()))
        return (
            max(1, round(Theme.cover_editor_width * dpr)),
            max(1, round(Theme.cover_editor_adjust_height * dpr)),
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
        if current in {
            ShelfKey.ALL.value,
            ShelfKey.RECENT.value,
            ShelfKey.FAVOURITES.value,
            ShelfKey.HIDDEN.value,
        } or current.startswith("collection:"):
            self.sidebar.set_active(current)

    def _on_language_changed(self) -> None:
        """Refresh all static labels after the locale has been reloaded."""
        self.sidebar.refresh_labels()
        self.settings_view.refresh_labels()
        self.chrome.refresh_labels()
        self.chrome.set_action_menu(self.shelf_view.create_action_menu())
        self.shelf_view.toolbar.refresh_labels()
        self.cover_editor_overlay.refresh_labels()
        self.shelf_view.render()

    def _handle_hidden_space_changed(self) -> None:
        settings_vm = self._context.settings_viewmodel
        shelf_vm = self._context.shelf_viewmodel
        shelf_vm.set_hidden_space_initialized(settings_vm.hidden_space_initialized)
        shelf_vm.set_show_hidden_collection(settings_vm.show_hidden_collection)
        # State-flip can imply DB side effects (revert/reset), so reload.
        shelf_vm.load_books()
        self._refresh_sidebar_collections()

    def _refresh_sidebar_collections(self) -> None:
        # Sidebar only renders what is currently allowed under the Privacy
        # toggle. ``visible_collections`` already drops hidable rows when
        # the toggle is off; the Hidden item is shown/hidden in parallel
        # so the two halves of the Hidden Space surface stay in sync.
        vm = self._context.shelf_viewmodel
        self.sidebar.set_collections(vm.visible_collections)
        self.sidebar.set_hidden_visible(vm.show_hidden_collection)

    def _sync_chrome(self) -> None:
        self.chrome.set_view_mode(self._context.shelf_viewmodel.view_mode.value)
        self.chrome.set_sort(
            self._context.shelf_viewmodel.sort_field.value,
            self._context.shelf_viewmodel.sort_ascending,
        )

    def _sync_title_control_mode(self) -> None:
        settings_vm = self._context.settings_viewmodel
        self.title_bar.set_title_control_mode(
            force_non_macos_title_controls=bool(settings_vm.inspect_non_native_title_control),
        )

    def _handle_books_deleted(self, _book_uuids: tuple[str, ...]) -> None:
        self._refresh_sidebar_collections()

    def _handle_collections_changed(self, active_key: str | None = None) -> None:
        self._refresh_sidebar_collections()
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

    def _position_cover_editor_overlay(self) -> None:
        if not hasattr(self, "cover_editor_overlay"):
            return
        root = self.centralWidget()
        if root is None:
            return
        self.cover_editor_overlay.setGeometry(0, 0, root.width(), root.height())
        if self.cover_editor_overlay.isVisible():
            self.cover_editor_overlay.raise_()
        self._raise_dialog_overlay_if_visible()

    def _raise_dialog_overlay_if_visible(self) -> None:
        if hasattr(self, "dialog_overlay") and self.dialog_overlay.isVisible():
            self.dialog_overlay.raise_()

    def _show_hidden_space_lock_overlay(self, root: QWidget) -> None:
        # Build the overlay as a child of root so it lives in the same
        # coordinate space as ``dialog_overlay`` / ``settings_view``. The
        # shelf chrome stays interactive underneath visually but the
        # overlay swallows all input so the user can't reach it.
        self._lock_overlay = HiddenSpaceLockOverlay(
            root,
            hint=self._context.settings_viewmodel.hidden_space_hint,
            verify=self._context.settings_viewmodel.verify_hidden_space_password,
        )
        self._lock_overlay.verified.connect(self._unlock_hidden_space)
        self._lock_overlay.dismissed.connect(self._dismiss_hidden_space)
        self._position_lock_overlay()
        self._lock_overlay.show()
        self._lock_overlay.raise_()
        self._lock_overlay.focus_password()

    def _unlock_hidden_space(self) -> None:
        # Password verified — make sure both VMs reflect the persisted
        # state, refresh the sidebar, and remove the overlay.
        self._handle_hidden_space_changed()
        self._tear_down_lock_overlay()

    def _dismiss_hidden_space(self) -> None:
        # User pressed Hide on the lock screen. Flip the persisted toggle
        # off so next launch boots straight to the normal shelf, then
        # remove the overlay. Books stay marked hidden in storage.
        self._context.settings_viewmodel.set_show_hidden_collection(False)
        self._tear_down_lock_overlay()

    def _tear_down_lock_overlay(self) -> None:
        overlay = self._lock_overlay
        self._lock_overlay = None
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()

    def _position_lock_overlay(self) -> None:
        if self._lock_overlay is None:
            return
        root = self.centralWidget()
        if root is None:
            return
        self._lock_overlay.setGeometry(0, 0, root.width(), root.height())
        self._lock_overlay.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_settings_overlay()
        self._position_cover_editor_overlay()
        self._position_dialog_overlay()
        self._position_lock_overlay()
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
    return None if name.strip() else t("dialog.collection_name_required")


def _collection_uuid_from_key(collection_key: str) -> str:
    prefix = "collection:"
    return collection_key[len(prefix) :] if collection_key.startswith(prefix) else collection_key


def _format_byte_count(value: int) -> str:
    amount = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    displayed = float(amount)
    while displayed >= 1024.0 and unit_index < len(units) - 1:
        displayed /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{amount} {units[unit_index]}"
    return f"{displayed:.1f} {units[unit_index]}"
