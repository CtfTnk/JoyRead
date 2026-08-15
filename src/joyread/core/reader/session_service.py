"""Reader-facing document session helpers."""

from __future__ import annotations

from contextlib import nullcontext
import logging
from pathlib import Path
from typing import Protocol

from joyread.core.archive import ArchiveImageService, ArchiveImageSession, ArchiveOpenLimits
from joyread.core.archive.batching import MAX_SEQUENTIAL_BATCH_ITEMS
from joyread.core.archive.errors import ArchiveCancelled
from joyread.core.archive.models import (
    ArchiveCachePolicy,
    ArchiveConversionStatus,
    ArchivePasswordRequest,
    ArchivePasswordResponse,
)
from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS
from joyread.core.archive.service import ARCHIVE_EXTENSIONS
from joyread.core.reader.epub_session import EPUB_EXTENSIONS, EpubReaderSession, open_epub_session
from joyread.core.reader.models import ReaderPageImage
from joyread.core.reader.pdf import PDF_EXTENSIONS, PdfImageServicePort
from joyread.core.services.archive_cache_lease import ArchiveCacheLease


logger = logging.getLogger(__name__)


class ReaderImageSession(Protocol):
    """Common interface every image-style reader session implements.

    Archive (CBZ/CBR/ZIP/RAR/7Z) and PDF sessions both expose the same
    surface: a flat ``page_count`` plus blocking compatibility page helpers.
    The asynchronous Reader consumes these sessions through the Application
    ``ReaderDocumentSource`` adapter. Archive implementation stays in Core;
    the Qt PDF implementation lives in Infrastructure.
    """

    page_count: int

    def get_page(self, index: int):  # noqa: ANN201 - archive and PDF page models share attributes.
        ...

    def get_pages(self, indices):  # noqa: ANN001, ANN201 - accepts any iterable of page indexes.
        ...

    def get_dimensions(self, index: int) -> tuple[int, int] | None:
        ...


class ReaderSessionService:
    """Opens supported reader documents and loads bounded page payloads.

    Acts as a polymorphic dispatch layer: given any supported source
    path, it picks the archive or PDF backend and returns a session that
    speaks :class:`ReaderImageSession`. EPUB has its own session type
    handled by :class:`EpubSessionService` because the page model is
    different (DOM-based, not image-based).
    """

    def __init__(
        self,
        archive_image_service: ArchiveImageService,
        pdf_image_service: PdfImageServicePort | None = None,
    ) -> None:
        self._archive_image_service = archive_image_service
        self._pdf_image_service = pdf_image_service

    def open_document(
        self,
        path: str | Path,
        password: str | None = None,
        passwords: dict[str, str] | None = None,
        skipped_archives: set[str] | None = None,
        *,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
        cache_lease: ArchiveCacheLease | None = None,
    ) -> ReaderImageSession:
        suffix = Path(path).suffix.lower()
        logger.info("Opening reader document: path=%s suffix=%s", path, suffix)
        if suffix in ARCHIVE_EXTENSIONS:
            return self.open_archive(
                path,
                password=password,
                passwords=passwords,
                skipped_archives=skipped_archives,
                nested_archive_max_depth=nested_archive_max_depth,
                archive_global_file_max_depth=archive_global_file_max_depth,
                limits=limits,
                document_cache_key=document_cache_key,
                allow_persistent_cache=allow_persistent_cache,
                cache_lease=cache_lease,
            )
        if suffix in PDF_EXTENSIONS:
            if self._pdf_image_service is None:
                raise RuntimeError("No PDF image service is configured.")
            return self._pdf_image_service.open(path)
        raise ValueError(f"Unsupported reader format: {suffix or Path(path).name}")

    def open_epub(self, path: str | Path) -> EpubReaderSession:
        """Open an EPUB and return a chapter-flow session.

        Lives alongside ``open_archive`` / ``open_document`` so callers
        (the novel viewmodel, tests) can address it directly without
        going through the image-paged routing in ``open_document``.
        """
        return open_epub_session(path)

    def open_archive(
        self,
        path: str | Path,
        password: str | None = None,
        passwords: dict[str, str] | None = None,
        skipped_archives: set[str] | None = None,
        *,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
        cache_lease: ArchiveCacheLease | None = None,
    ) -> ArchiveImageSession:
        provider = None
        password_map = dict(passwords or {})
        skipped = set(skipped_archives or ())
        if password is not None:
            password_map.setdefault(str(Path(path)), password)
        if password_map or skipped:
            # Adapt the reader's simple "password dict + skipped set" inputs
            # to the full ``PasswordProvider`` protocol the archive service
            # expects. The reader VM never touches the protocol type
            # directly — keeping that translation here so callers stay
            # ignorant of the archive-service API shape.
            provider = lambda request: (
                ArchivePasswordResponse(skip=True)
                if request.archive_path in skipped
                else password_map.get(request.archive_path)
            )
        return self._archive_image_service.open(
            path,
            password_provider=provider,
            max_nested_depth=nested_archive_max_depth,
            global_file_max_depth=archive_global_file_max_depth,
            limits=limits,
            document_cache_key=document_cache_key,
            allow_persistent_cache=allow_persistent_cache,
            cache_lease=cache_lease,
        )

    def load_page(self, session: ReaderImageSession, page_index: int) -> ReaderPageImage | None:
        page = session.get_page(page_index)
        if page is None:
            return None
        loaded_index = getattr(page, "index", getattr(page, "page_index", None))
        if loaded_index is None:
            return None
        return ReaderPageImage(page_index=int(loaded_index), image_bytes=page.image_bytes, dimensions=page.dimensions)

    def load_pages(
        self,
        session: ReaderImageSession,
        page_indices: list[int] | tuple[int, ...],
    ) -> dict[int, ReaderPageImage]:
        read_pages = getattr(session, "read_pages", session.get_pages)
        pages = read_pages(page_indices)
        loaded: dict[int, ReaderPageImage] = {}
        for page in pages:
            if page is None:
                continue
            page_index = getattr(page, "index", getattr(page, "page_index", None))
            if page_index is None:
                continue
            loaded[int(page_index)] = ReaderPageImage(
                page_index=int(page_index),
                image_bytes=page.image_bytes,
                dimensions=page.dimensions,
            )
        return loaded

    def warm_disk_cache(
        self,
        path: str | Path,
        *,
        password: str | None = None,
        nested_archive_max_depth: int | None = None,
        archive_global_file_max_depth: int | None = None,
        limits: ArchiveOpenLimits | None = None,
        document_cache_key: str | None = None,
        allow_persistent_cache: bool = False,
        cache_lease: ArchiveCacheLease | None = None,
        chunk_size: int = 8,
        is_cancelled=None,  # noqa: ANN001 - accepts TaskHandle-like status checks.
    ) -> None:
        """Warm extracted-page disk cache in forward page order.

        The reader keeps its own foreground session. Warm-up opens a separate
        archive session so slow whole-book extraction never holds the visible
        reader's session lock or blocks current/nearby page loads.
        """

        logger.debug("Warming disk cache for %s", path)
        session = self.open_archive(
            path,
            password=password,
            nested_archive_max_depth=nested_archive_max_depth,
            archive_global_file_max_depth=archive_global_file_max_depth,
            limits=limits,
            document_cache_key=document_cache_key,
            allow_persistent_cache=allow_persistent_cache,
            cache_lease=cache_lease,
        )
        try:
            plan = getattr(session, "cache_plan", None)
            policy = getattr(plan, "policy", None)
            builds_cache = policy not in {
                ArchiveCachePolicy.ON_DEMAND_ONLY,
                ArchiveCachePolicy.DIRECT,
            }
            guard = (
                session.cache_build_guard()
                if builds_cache
                else nullcontext(True)
            )
            with guard as build_registered:
                if not build_registered:
                    logger.warning("Disk cache warm could not register its build for %s", path)
                    return
                if not self._warm_under_policy(
                    session,
                    path,
                    chunk_size=chunk_size,
                    is_cancelled=is_cancelled,
                ):
                    return
                mark_ready = getattr(session, "mark_thumbnail_cache_ready", None)
                if callable(mark_ready):
                    mark_ready()
        except ArchiveCancelled:
            # Cancellation is the coordinator dropping its last consumer, not a
            # failure worth reporting up as one.
            logger.debug("Disk cache warm cancelled for %s", path)
            return
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        logger.debug("Disk cache warm complete for %s", path)

    def _warm_under_policy(
        self,
        session: ReaderImageSession,
        path: str | Path,
        *,
        chunk_size: int,
        is_cancelled,  # noqa: ANN001 - accepts TaskHandle-like status checks.
    ) -> bool:
        """Do whatever this document's cache policy allows.

        Returns whether the document finished warming and may now be marked
        ready. The policy -- not the conversion result -- decides whether any
        background work runs at all, so a document refused a whole-document
        cache product cannot reach the same product through the slower path.
        """

        plan = getattr(session, "cache_plan", None)
        policy = getattr(plan, "policy", None)
        if policy is ArchiveCachePolicy.ON_DEMAND_ONLY:
            logger.info(
                "Skipping background warmup for %s: on-demand only (%s). Foreground "
                "reads stay available and are not persisted.",
                path,
                getattr(plan, "reason", ""),
            )
            self._discard_unpublished_cache(session)
            return False
        if policy is ArchiveCachePolicy.DIRECT:
            return False
        if policy is ArchiveCachePolicy.BULK_CONVERT:
            done = self._convert_whole_document(session, path, is_cancelled=is_cancelled)
            if done is not None:
                return done
        return self._warm_in_chunks(
            session,
            chunk_size=chunk_size,
            is_cancelled=is_cancelled,
        )

    def _convert_whole_document(
        self,
        session: ReaderImageSession,
        path: str | Path,
        *,
        is_cancelled,  # noqa: ANN001 - accepts TaskHandle-like status checks.
    ) -> bool | None:
        """Run one whole-document conversion.

        Returns ``True`` when the document is warmed, ``False`` when the warmup
        must stop without it, and ``None`` when the backend turned out not to
        support this container after all, which is the only case that may drop
        to bounded sequential warming. A conversion that ran and failed must not
        be retried the slow way: the sequential path would re-read the same
        input and walk straight past whatever stopped the conversion. Guardrail
        outcomes never arrive here as a result -- they are raised.
        """

        convert = getattr(session, "convert_to_cache", None)
        if not callable(convert):
            return None
        result = convert(is_cancelled=is_cancelled)
        if result.is_published:
            logger.info(
                "Bulk-converted %s to the extraction cache: pages=%d status=%s",
                path,
                result.pages,
                result.status.value,
            )
            return True
        if result.status is ArchiveConversionStatus.UNSUPPORTED:
            logger.debug(
                "Bulk conversion unavailable for %s (%s); warming in bounded batches",
                path,
                result.reason,
            )
            return None
        if result.status is ArchiveConversionStatus.ON_DEMAND_ONLY:
            self._discard_unpublished_cache(session)
            return False
        logger.warning(
            "Bulk conversion failed for %s (%s); not falling back to sequential warmup",
            path,
            result.reason,
        )
        return False

    @staticmethod
    def _discard_unpublished_cache(session: ReaderImageSession) -> None:
        """Reclaim a partial bundle this document may never complete.

        Earlier builds warmed every document into the shared cache, so an
        on-demand-only document can still own a partial bundle that will now
        never be finished or evicted by its own progress. Any published bundle
        is left alone -- including one built under different limits: it is a
        complete, readable product for whichever reader is using them.
        """

        discard = getattr(session, "discard_unpublished_cache", None)
        if callable(discard):
            discard()

    def _warm_in_chunks(
        self,
        session: ReaderImageSession,
        *,
        chunk_size: int,
        is_cancelled,  # noqa: ANN001 - accepts TaskHandle-like status checks.
    ) -> bool:
        """Read every page in forward order. Returns whether it ran to the end."""

        pending = tuple(range(session.page_count))
        chunk_size = max(1, int(chunk_size))
        completed = 0
        planner = getattr(session, "plan_read_batch", None)
        while pending:
            if is_cancelled is not None and is_cancelled():
                logger.debug("Disk cache warm cancelled after pages=%d", completed)
                return False
            if callable(planner):
                candidate_limit = min(chunk_size, MAX_SEQUENTIAL_BATCH_ITEMS)
                batch = tuple(
                    planner(
                        pending[:candidate_limit],
                        max_items=candidate_limit,
                    )
                )
            else:
                batch = pending[:chunk_size]
            if not batch or batch != pending[: len(batch)]:
                batch = pending[:1]
            self.load_pages(session, batch)
            pending = pending[len(batch) :]
            completed += len(batch)
        return True

    def password_request_label(self, request: ArchivePasswordRequest) -> str:
        return f"{request.archive_format} archive password"
