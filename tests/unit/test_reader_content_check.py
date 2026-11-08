"""The Reader asks before reading an archive that is mostly not pages.

Covers the three layers separately: the scanner's content profile, the
count-based rule in ``core.reader.content_check``, and the ViewModel holding an
opened document back until the user answers. The Reader window test at the end
drives the real dialog.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pyzipper
import pytest
from PIL import Image

from joyread.app.reader_document_runtime import ReaderDocumentRuntime
from joyread.app.tasking import TaskHandle, TaskStatus
from joyread.core.archive import (
    ArchiveContentProfile,
    ArchiveImageService,
    ArchiveOpenLimits,
    ArchivePasswordPolicy,
)
from joyread.core.reader import ReaderPageImage
from joyread.core.reader.content_check import (
    ContentWarningMemory,
    UnshownGroup,
    UnshownKind,
    assess_archive_content,
)
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool
from joyread.core.services.cache_service import CacheService
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.reader_content_prompt import build_content_prompt
from joyread.ui.viewmodels.reader_viewmodel import ReaderViewModel


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (24, 32), "#336699").save(buffer, format="PNG")
    return buffer.getvalue()


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def _encrypted_zip() -> bytes:
    buffer = BytesIO()
    with pyzipper.AESZipFile(buffer, "w", compression=8, encryption=pyzipper.WZ_AES) as archive:
        archive.setpassword(b"pw")
        archive.writestr("001.png", _png())
    return buffer.getvalue()


def _pages(count: int, folder: str = "") -> list[tuple[str, bytes]]:
    page = _png()
    return [(f"{folder}{index:03d}.png", page) for index in range(count)]


def _write(path: Path, members: list[tuple[str, bytes]]) -> Path:
    path.write_bytes(_zip_bytes(members))
    return path


def _profile(path: Path, **open_kwargs) -> tuple[ArchiveContentProfile, int]:  # noqa: ANN003
    session = ArchiveImageService().open(path, password_policy=ArchivePasswordPolicy.SKIP, **open_kwargs)
    try:
        assert session.content_profile is not None
        return session.content_profile, session.page_count
    finally:
        session.close()


# -- Scanner profile -------------------------------------------------------


def test_profile_counts_pages_and_leaves_junk_and_metadata_out(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "comic.cbz",
        _pages(4)
        + [
            ("ComicInfo.xml", b"<x/>"),
            ("credits.txt", b"thanks"),
            ("Thumbs.db", b""),
            ("__MACOSX/._000.png", b""),
            (".DS_Store", b""),
        ],
    )

    profile, page_count = _profile(path)

    assert profile.pages == page_count == 4
    assert profile.metadata_files == 3
    assert profile.unshown == 0
    assert profile.shown_fraction == 1.0


def test_profile_counts_unsupported_types_by_suffix(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "photos.zip",
        _pages(2)
        + [(f"IMG_{index}.HEIC", b"x") for index in range(5)]
        + [("clip.MP4", b"x"), ("clip2.mp4", b"x"), ("README", b"x")],
    )

    profile, _ = _profile(path)

    assert profile.pages == 2
    assert profile.other_files == 8
    # Case-folded, most frequent first, at most three kept by name.
    assert profile.top_other_suffixes == ((".heic", 5), (".mp4", 2), ("", 1))


def test_profile_reaches_into_nested_archives_and_counts_what_it_left_out(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bundle.zip",
        [
            ("v1.cbz", _zip_bytes(_pages(3) + [("art.psd", b"x")])),
            ("extras/locked.zip", _encrypted_zip()),
            ("broken.zip", b"not an archive"),
        ],
    )

    profile, page_count = _profile(path)

    # The opened volume counts its contents, not itself.
    assert profile.pages == page_count == 3
    assert profile.other_files == 1
    assert profile.locked_archives == 1
    assert profile.skipped_archives == 1


def test_profile_counts_what_the_depth_limits_put_out_of_reach(tmp_path: Path) -> None:
    inner = _zip_bytes(_pages(2))
    path = _write(
        tmp_path / "deep.zip",
        _pages(1) + _pages(2, folder="a/b/c/") + [("nested.zip", _zip_bytes([("again.zip", inner)]))],
    )
    limits = ArchiveOpenLimits(nested_archive_max_depth=1, global_file_max_depth=2)

    profile, page_count = _profile(path, limits=limits)

    assert profile.pages == page_count == 1
    assert profile.out_of_reach == 2
    # ``again.zip`` sits one nesting level past the limit.
    assert profile.skipped_archives == 1


# -- The rule --------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "other", "warns"),
    [
        (24, 0, False),
        (20, 2, False),
        (6, 30, True),
        (8, 12, True),
        # Exactly half is not "fewer than half".
        (3, 3, False),
        # Under half, but too few files left out to be worth a question.
        (1, 2, False),
        (2, 3, True),
    ],
)
def test_rule_warns_below_half_with_at_least_three_unshown(pages: int, other: int, warns: bool) -> None:
    profile = ArchiveContentProfile(pages=pages, other_files=other, top_other_suffixes=((".dat", other),))

    assert (assess_archive_content(profile) is not None) is warns


def test_rule_leaves_empty_archives_and_pdfs_to_their_own_paths() -> None:
    assert assess_archive_content(None) is None
    assert assess_archive_content(ArchiveContentProfile(pages=0, other_files=40)) is None


def test_rule_names_the_three_largest_groups_and_folds_the_rest() -> None:
    profile = ArchiveContentProfile(
        pages=2,
        other_files=33,
        locked_archives=2,
        out_of_reach=1,
        top_other_suffixes=((".heic", 20), (".mov", 8), (".aae", 4)),
    )

    warning = assess_archive_content(profile)

    assert warning is not None
    assert warning.countable == 38
    assert warning.shown_percent == 5
    assert warning.groups == (
        UnshownGroup(UnshownKind.FILE_TYPE, 20, "HEIC"),
        UnshownGroup(UnshownKind.FILE_TYPE, 8, "MOV"),
        UnshownGroup(UnshownKind.FILE_TYPE, 4, "AAE"),
        # One more file type past the profile's top three, two locked
        # archives and one deep page.
        UnshownGroup(UnshownKind.OTHER_FILES, 4),
    )


def test_prompt_wording_uses_counts_and_the_archive_name_only() -> None:
    profile = ArchiveContentProfile(
        pages=6, other_files=30, locked_archives=1, top_other_suffixes=((".heic", 30),)
    )
    warning = assess_archive_content(profile)
    assert warning is not None

    prompt = build_content_prompt(warning, "phone-photos.zip")

    assert prompt.title == t("reader.content_warning_title")
    assert prompt.message == (
        "Only 6 of 37 files in “phone-photos.zip” are pages JoyRead can show (16%). "
        "The rest: 30 HEIC files, 1 password-protected archive. Read it anyway?"
    )


def test_memory_is_keyed_by_path_and_file_state() -> None:
    memory = ContentWarningMemory()
    path = Path("/books/a.zip")

    memory.remember(path, (10, 20, 30))

    assert memory.was_accepted(path, (10, 20, 30))
    assert not memory.was_accepted(path, (11, 20, 30))
    assert not memory.was_accepted(Path("/books/b.zip"), (10, 20, 30))
    # A file that could not be stat'ed is never remembered or matched.
    memory.remember(path, None)
    assert not memory.was_accepted(path, None)


# -- ViewModel -------------------------------------------------------------


class _ProfiledSession:
    page_count = 6
    contents = ()

    def __init__(self, profile: ArchiveContentProfile) -> None:
        self.content_profile = profile
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProfiledSessionService:
    def __init__(self, profile: ArchiveContentProfile) -> None:
        self._profile = profile
        self.sessions: list[_ProfiledSession] = []
        self.page_requests: list[tuple[int, ...]] = []

    def open_document(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
        session = _ProfiledSession(self._profile)
        self.sessions.append(session)
        return session

    def load_pages(self, _session, page_indices: tuple[int, ...]):  # noqa: ANN001, ANN201
        self.page_requests.append(page_indices)
        return {index: ReaderPageImage(index, _png(), (24, 32)) for index in page_indices}


class _SyncTaskService:
    def submit(self, name, callback, *, on_success=None, on_failure=None, **_kwargs):  # noqa: ANN001, ANN003, ANN201
        handle = TaskHandle(task_id=name)
        try:
            result = callback()
        except Exception as exc:  # pragma: no cover - test helper only.
            handle.status = TaskStatus.FAILED
            handle.error = exc
            if on_failure is not None:
                on_failure(exc)
            return handle
        handle.status = TaskStatus.COMPLETED
        handle.result = result
        if on_success is not None:
            on_success(result)
        return handle


_PHOTOS = ArchiveContentProfile(pages=6, other_files=30, top_other_suffixes=((".heic", 30),))


def _viewmodel(
    tmp_path: Path,
    service: _ProfiledSessionService,
    *,
    check: bool = True,
    memory: ContentWarningMemory | None = None,
) -> ReaderViewModel:
    pool = ArchiveExtractionPool(tmp_path / "pool", max_bytes=4 * 1024 * 1024)
    cache = CacheService(archive_extraction_pool=pool, reader_page_cache_max_bytes=4 * 1024 * 1024)
    vm = ReaderViewModel(
        ReaderDocumentRuntime(service),  # type: ignore[arg-type]
        _SyncTaskService(),  # type: ignore[arg-type]
        cache.issue_reader_namespace(),
        title="photos",
        check_archive_content=check,
        content_warning_memory=memory,
    )
    vm.set_viewport_size(1200, 900)
    return vm


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "phone-photos.zip"
    path.write_bytes(b"stand-in")
    return path


def test_viewmodel_holds_the_document_until_the_user_answers(tmp_path: Path) -> None:
    service = _ProfiledSessionService(_PHOTOS)
    vm = _viewmodel(tmp_path, service)
    prompts = []
    vm.content_warning_required.connect(prompts.append)

    vm.open_path(_source(tmp_path))

    assert len(prompts) == 1
    assert "phone-photos.zip" in prompts[0].message
    assert vm.awaiting_content_decision
    assert vm.is_loading
    assert vm.page_count == 0
    assert service.page_requests == []

    vm.proceed_after_content_warning()

    assert not vm.awaiting_content_decision
    assert vm.page_count == 6
    assert service.page_requests


def test_declining_closes_the_document_without_reading_a_page(tmp_path: Path) -> None:
    service = _ProfiledSessionService(_PHOTOS)
    vm = _viewmodel(tmp_path, service)

    vm.open_path(_source(tmp_path))
    vm.decline_content_warning()

    assert not vm.awaiting_content_decision
    assert service.sessions[0].closed
    assert service.page_requests == []
    assert vm.page_count == 0


def test_closing_the_reader_while_asking_closes_the_held_document(tmp_path: Path) -> None:
    service = _ProfiledSessionService(_PHOTOS)
    vm = _viewmodel(tmp_path, service)

    vm.open_path(_source(tmp_path))
    vm.cancel()

    assert service.sessions[0].closed
    assert not vm.awaiting_content_decision


def test_read_anyway_is_remembered_for_the_same_unchanged_file(tmp_path: Path) -> None:
    memory = ContentWarningMemory()
    source = _source(tmp_path)
    first = _viewmodel(tmp_path, _ProfiledSessionService(_PHOTOS), memory=memory)
    first.open_path(source)
    first.proceed_after_content_warning()
    first.cancel()

    again = _viewmodel(tmp_path, _ProfiledSessionService(_PHOTOS), memory=memory)
    prompts = []
    again.content_warning_required.connect(prompts.append)
    again.open_path(source)
    assert prompts == []
    assert again.page_count == 6
    again.cancel()

    source.write_bytes(b"a different file now")
    changed = _viewmodel(tmp_path, _ProfiledSessionService(_PHOTOS), memory=memory)
    changed.content_warning_required.connect(prompts.append)
    changed.open_path(source)
    assert len(prompts) == 1


def test_no_question_when_the_check_does_not_apply(tmp_path: Path) -> None:
    service = _ProfiledSessionService(_PHOTOS)
    vm = _viewmodel(tmp_path, service, check=False)
    prompts = []
    vm.content_warning_required.connect(prompts.append)

    vm.open_path(_source(tmp_path))

    assert prompts == []
    assert vm.page_count == 6


def test_the_log_records_counts_but_no_names(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    service = _ProfiledSessionService(_PHOTOS)
    vm = _viewmodel(tmp_path, service)

    with caplog.at_level("INFO"):
        vm.open_path(_source(tmp_path))
        vm.decline_content_warning()

    records = [r for r in caplog.records if getattr(r, "event", None) == "reader.document.content_warning"]
    assert [r.outcome for r in records] == ["asked", "declined"]
    assert (records[0].count, records[0].total, records[0].percent) == (6, 36, 16)
    for record in records:
        assert "phone-photos" not in record.getMessage()
        assert all("phone-photos" not in str(value) for value in vars(record).values())


# -- Reader window ---------------------------------------------------------


def _photo_dump(tmp_path: Path) -> Path:
    return _write(tmp_path / "photos.zip", _pages(2) + [(f"IMG_{i}.HEIC", b"x") for i in range(10)])


def test_reader_window_asks_and_closes_on_cancel(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    from joyread.app.app_context import create_app_context
    from joyread.ui.views.reader_window import ReaderWindow

    context = create_app_context()
    try:
        window = ReaderWindow(context, _photo_dump(tmp_path), check_archive_content=True)
        qtbot.addWidget(window)
        window.show()
        qtbot.waitUntil(lambda: window.dialog_overlay.isVisible(), timeout=5000)

        assert window.dialog_overlay._panel.title_text == t("reader.content_warning_title")
        assert window.viewmodel.awaiting_content_decision

        with qtbot.waitSignal(window.closed, timeout=2000):
            window.dialog_overlay._reject()
    finally:
        context.close()


def test_reader_window_reads_after_read_anyway(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    from joyread.app.app_context import create_app_context
    from joyread.ui.views.reader_window import ReaderWindow

    context = create_app_context()
    try:
        window = ReaderWindow(context, _photo_dump(tmp_path), check_archive_content=True)
        qtbot.addWidget(window)
        window.show()
        qtbot.waitUntil(lambda: window.dialog_overlay.isVisible(), timeout=5000)

        window.dialog_overlay._accept()

        qtbot.waitUntil(lambda: window.viewmodel.page_count == 2, timeout=5000)
        assert not window.dialog_overlay.isVisible()
        window.close()
    finally:
        context.close()


def test_reader_window_for_a_library_book_never_asks(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    from joyread.app.app_context import create_app_context
    from joyread.ui.views.reader_window import ReaderWindow

    context = create_app_context()
    try:
        # Without the flag, as every Library and Open & Import request is built.
        window = ReaderWindow(context, _photo_dump(tmp_path))
        qtbot.addWidget(window)
        window.show()

        qtbot.waitUntil(lambda: window.viewmodel.page_count == 2, timeout=5000)
        assert not window.dialog_overlay.isVisible()
        window.close()
    finally:
        context.close()
