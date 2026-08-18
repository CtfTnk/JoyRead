"""Chapter-flow viewmodel driving the novel reader shell.

Mirrors the public signal surface of :class:`ReaderViewModel` so the
chrome wiring stays symmetric across manga and novel windows. Persists
progress + bookmarks against the existing manga schema (``page_index``
column reused for ``spine_index`` — see plan §7).
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path

from joyread.core.reader.epub import EpubAssetReader, EpubBook, flatten_toc
from joyread.core.reader.epub_session import EpubChapter, EpubReaderSession
from joyread.app.tasking import TaskExecutor, TaskHandle
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.reader_items import (
    ReaderBookmarkItem,
    ReaderContentsItem,
    ReaderPasswordPrompt,
)
from joyread.ui.viewmodels.signals import Signal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NovelChapterPayload:
    """Bundle handed to the content widget on every successful seek."""

    html: str
    asset_reader: EpubAssetReader
    base_dir: str
    spine_index: int
    chapter_title: str
    language: str | None
    chapter_count: int


class NovelReaderViewModel:
    """Coordinates EPUB session, TOC, bookmarks, and progress."""

    def __init__(
        self,
        task_service: TaskExecutor,
        library_service=None,  # noqa: ANN001 - LibraryService import cycle
        *,
        book_uuid: str | None = None,
        title: str = "Novel",
        initial_spine_index: int = 0,
    ) -> None:
        self.state_changed: Signal = Signal()
        self.chapter_rendered: Signal = Signal()  # (NovelChapterPayload)
        self.toc_changed: Signal = Signal()  # (tuple[ReaderContentsItem, ...])
        self.bookmarks_changed: Signal = Signal()  # (tuple[ReaderBookmarkItem, ...])
        self.bookmark_error_changed: Signal = Signal()  # (str)
        self.progress_changed: Signal = Signal()  # (book_uuid, spine_index, percent)
        self.error_changed: Signal = Signal()  # (str | None)
        # Plumbed for the future encryption path; not fired in this milestone.
        self.password_required: Signal = Signal()  # (ReaderPasswordPrompt)
        self.writing_mode_warning: Signal = Signal()  # (str) — emitted once if vertical-rl

        self._task_service = task_service
        self._library_service = library_service
        self._book_uuid = book_uuid

        self.title = title
        self.current_index: int = max(0, int(initial_spine_index))
        self.chapter_count: int = 0
        self.is_loading: bool = False
        self.error_message: str | None = None
        self.writing_mode: str | None = None

        self._session: EpubReaderSession | None = None
        self._source_path: Path | None = None
        self._bookmarks: tuple[ReaderBookmarkItem, ...] = ()
        # Bumped on every cancel() / open_path(); late callbacks compare
        # against the captured snapshot and drop their result if the
        # generation rolled forward. Mirrors the manga viewmodel's pattern.
        self._task_generation = 0
        self._open_handle: TaskHandle[EpubReaderSession] | None = None
        self._chapter_handle: TaskHandle[EpubChapter] | None = None
        self._progress_handle: TaskHandle[None] | None = None
        self._bookmark_handle = None
        self._last_saved_progress: tuple[int, float] | None = None
        self._writing_mode_warning_emitted = False

    # --- Public API: lifecycle ----------------------------------------------

    @property
    def can_use_bookmarks(self) -> bool:
        return self._library_service is not None and self._book_uuid is not None

    @property
    def can_use_contents(self) -> bool:
        return self._session is not None and bool(self._session.toc)

    def open_path(self, source_path: Path, password: str | None = None) -> None:
        del password  # encryption is plumbed but not implemented
        self._task_generation += 1
        generation = self._task_generation
        self._cancel_handles()
        self._source_path = Path(source_path)
        self.is_loading = True
        self.error_message = None
        self.state_changed.emit()
        self.error_changed.emit(None)
        path = self._source_path

        def runner() -> EpubReaderSession:
            from joyread.core.reader.epub_session import open_epub_session

            return open_epub_session(path)

        self._open_handle = self._task_service.submit(
            "open-epub",
            runner,
            on_success=lambda session, generation=generation: self._handle_open_success(session, generation),
            on_failure=lambda exc, generation=generation: self._handle_open_failure(exc, generation),
        )

    def cancel(self) -> None:
        self._task_generation += 1
        self._cancel_handles()
        session = self._session
        if session is not None:
            try:
                session.close()
            except Exception:
                logger.exception("Closing EPUB session failed")
        self._session = None

    # --- Public API: navigation --------------------------------------------

    def seek(self, spine_index: int) -> None:
        if self._session is None:
            return
        index = max(0, min(self._session.chapter_count - 1, int(spine_index)))
        if index == self.current_index and self._chapter_handle is None:
            # Re-emit cached chapter so the content area can re-render
            # (e.g. when toggling Enable Custom and we want the new CSS).
            self._emit_chapter(self._session.load_chapter(index))
            return
        self._load_chapter(index)

    def go_next(self) -> None:
        if self._session is None:
            return
        if self.current_index + 1 < self._session.chapter_count:
            self.seek(self.current_index + 1)

    def go_previous(self) -> None:
        if self._session is None:
            return
        if self.current_index > 0:
            self.seek(self.current_index - 1)

    def jump_to_start(self) -> None:
        if self._session is None:
            return
        self.seek(0)

    def jump_to_end(self) -> None:
        if self._session is None:
            return
        self.seek(self._session.chapter_count - 1)

    # --- Public API: bookmarks ---------------------------------------------

    def refresh_bookmarks(self) -> None:
        if not self.can_use_bookmarks:
            return
        book_uuid = self._book_uuid
        self._bookmark_handle = self._task_service.submit(
            "novel-bookmarks-load",
            lambda: self._library_service.list_bookmarks(book_uuid),
            on_success=self._apply_bookmarks,
            on_failure=lambda exc: self._emit_bookmark_error(t("reader.bookmark_load_failed"), exc),
        )

    def add_bookmark(self, name: str | None = None) -> None:
        if not self.can_use_bookmarks or self._session is None:
            return
        spine_index = self.current_index
        label = (
            name
            or self._session.chapter_title(spine_index)
            or t("reader.chapter_fallback", index=str(spine_index + 1))
        )
        book_uuid = self._book_uuid
        self._bookmark_handle = self._task_service.submit(
            "novel-bookmark-add",
            lambda: self._library_service.add_bookmark(book_uuid, label, spine_index),
            on_success=lambda _result: self.refresh_bookmarks(),
            on_failure=lambda exc: self._emit_bookmark_error(t("reader.bookmark_add_failed"), exc),
        )

    def delete_bookmark(self, bookmark_uuid: str) -> None:
        if not self.can_use_bookmarks:
            return
        book_uuid = self._book_uuid
        self._bookmark_handle = self._task_service.submit(
            "novel-bookmark-delete",
            lambda: self._library_service.delete_bookmark(book_uuid, bookmark_uuid),
            on_success=lambda _result: self.refresh_bookmarks(),
            on_failure=lambda exc: self._emit_bookmark_error(t("reader.bookmark_delete_failed"), exc),
        )

    def rename_bookmark(self, bookmark_uuid: str, new_name: str) -> None:
        if not self.can_use_bookmarks:
            return
        cleaned = (new_name or "").strip()
        if not cleaned:
            self._emit_bookmark_error(t("reader.bookmark_name_required"), None)
            return
        book_uuid = self._book_uuid
        self._bookmark_handle = self._task_service.submit(
            "novel-bookmark-rename",
            lambda: self._library_service.rename_bookmark(book_uuid, bookmark_uuid, cleaned),
            on_success=lambda _result: self.refresh_bookmarks(),
            on_failure=lambda exc: self._emit_bookmark_error(t("reader.bookmark_rename_failed"), exc),
        )

    # --- Internals: open / chapter loading ----------------------------------

    def _handle_open_success(self, session: EpubReaderSession, generation: int) -> None:
        if generation != self._task_generation:
            session.close()
            return
        self._session = session
        self.chapter_count = session.chapter_count
        self.writing_mode = session.book.metadata.primary_writing_mode
        # Anchor title/current_index to the book metadata before the first
        # chapter renders so the chrome's first paint reflects reality.
        if session.book.metadata.title:
            self.title = session.book.metadata.title
        self.current_index = max(0, min(self.current_index, session.chapter_count - 1))
        self.is_loading = False
        self.state_changed.emit()

        # Emit TOC, then bookmarks, then load the resume chapter.
        toc_items = _toc_to_contents_items(session.book)
        self.toc_changed.emit(toc_items)
        if self.can_use_bookmarks:
            self.refresh_bookmarks()
        if (
            self.writing_mode == "vertical-rl"
            and not self._writing_mode_warning_emitted
        ):
            self._writing_mode_warning_emitted = True
            self.writing_mode_warning.emit(
                t("reader.vertical_writing_unsupported")
            )
        self._load_chapter(self.current_index)

    def _handle_open_failure(self, exc: BaseException, generation: int) -> None:
        if generation != self._task_generation:
            return
        self.is_loading = False
        message = t("reader.epub_open_failed", error=str(exc))
        self.error_message = message
        logger.error("EPUB open failed: %s", exc, exc_info=True)
        self.state_changed.emit()
        self.error_changed.emit(message)

    def _load_chapter(self, spine_index: int) -> None:
        if self._session is None:
            return
        session = self._session
        generation = self._task_generation

        def runner() -> EpubChapter:
            return session.load_chapter(spine_index)

        self._chapter_handle = self._task_service.submit(
            "novel-load-chapter",
            runner,
            on_success=lambda chapter, generation=generation: self._handle_chapter_loaded(chapter, generation),
            on_failure=lambda exc, generation=generation: self._handle_chapter_failure(exc, generation),
        )

    def _handle_chapter_loaded(self, chapter: EpubChapter, generation: int) -> None:
        if generation != self._task_generation or self._session is None:
            return
        self.current_index = chapter.spine_index
        self.error_message = None
        self.state_changed.emit()
        self._emit_chapter(chapter)
        self._save_progress()

    def _handle_chapter_failure(self, exc: BaseException, generation: int) -> None:
        if generation != self._task_generation:
            return
        message = t("reader.chapter_load_failed", error=str(exc))
        self.error_message = message
        logger.error("EPUB load_chapter failed: %s", exc, exc_info=True)
        self.state_changed.emit()
        self.error_changed.emit(message)

    def _emit_chapter(self, chapter: EpubChapter) -> None:
        if self._session is None:
            return
        # ``base_dir`` is the chapter file's own directory, not the OPF
        # directory. Many EPUBs (e.g. Okaasan) place chapters under
        # ``OEBPS/Text/`` while images live in ``OEBPS/Images/``; the
        # chapter's HTML uses ``../Images/foo.jpg`` which must resolve
        # against the chapter file location, not the OPF root.
        chapter_dir = posixpath.dirname(chapter.href) or self._session.book.base_dir
        payload = NovelChapterPayload(
            html=chapter.html,
            asset_reader=self._session.asset_reader,
            base_dir=chapter_dir,
            spine_index=chapter.spine_index,
            chapter_title=chapter.title,
            language=chapter.language,
            chapter_count=self._session.chapter_count,
        )
        self.chapter_rendered.emit(payload)

    # --- Internals: persistence --------------------------------------------

    def _save_progress(self) -> None:
        if self._library_service is None or self._book_uuid is None or self._session is None:
            return
        chapter_count = self._session.chapter_count
        if chapter_count <= 0:
            return
        page_index = self.current_index
        # Chapter-level resume only in this milestone; within-chapter
        # scroll is deliberately not persisted (plan §7).
        percent = 0.0 if chapter_count <= 1 else (page_index / (chapter_count - 1)) * 100.0
        snapshot = (page_index, percent)
        if self._last_saved_progress == snapshot:
            return
        self._last_saved_progress = snapshot
        book_uuid = self._book_uuid
        self._progress_handle = self._task_service.submit(
            "novel-save-progress",
            lambda: self._library_service.set_progress(book_uuid, page_index, percent),
            on_success=lambda _result: self.progress_changed.emit(book_uuid, page_index, percent),
        )

    def _apply_bookmarks(self, bookmarks: tuple[ReaderBookmarkItem, ...]) -> None:
        self._bookmarks = tuple(bookmarks)
        self.bookmarks_changed.emit(self._bookmarks)

    def _emit_bookmark_error(self, message: str, exc: BaseException | None) -> None:
        if exc is not None:
            logger.warning("%s (%s)", message, exc)
        self.bookmark_error_changed.emit(message)

    def _cancel_handles(self) -> None:
        for handle_attr in ("_open_handle", "_chapter_handle", "_progress_handle", "_bookmark_handle"):
            handle = getattr(self, handle_attr, None)
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass
                setattr(self, handle_attr, None)


def _toc_to_contents_items(book: EpubBook) -> tuple[ReaderContentsItem, ...]:
    flat = flatten_toc(book.toc)
    items: list[ReaderContentsItem] = []
    for entry in flat:
        if entry.spine_index < 0:
            continue
        items.append(
            ReaderContentsItem(
                label=entry.label,
                page_index=entry.spine_index,
                depth=entry.depth,
            )
        )
    return tuple(items)


__all__ = [
    "NovelChapterPayload",
    "NovelReaderViewModel",
    "ReaderPasswordPrompt",
]
