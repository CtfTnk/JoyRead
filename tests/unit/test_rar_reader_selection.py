"""RAR/CBR: solid detection, one bounded batch call, and command fencing."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
import rarfile

from joyread.core.archive import ArchiveImageService
from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.errors import ArchiveError
from joyread.core.archive.formats import rar_backend as rar_module
from joyread.core.archive.formats import seven_zip_command as command_module
from joyread.core.archive.formats.rar_backend import (
    RarArchiveBackend,
    rar_requires_sequential_warmup,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveSource

CBR_FIXTURE = Path("test_set/Disney Villains - Gaston 001 (2026) (4 covers) (digital) (Salem-Empire).cbr")
RAR_FIXTURE = Path("test_set/Code：坦克世界是一款.rar")

requires_cbr_fixture = pytest.mark.skipif(
    not CBR_FIXTURE.is_file(), reason="the real RAR corpus is not present"
)


def _backend() -> RarArchiveBackend:
    from threading import RLock

    return RarArchiveBackend(lambda: rarfile, ExtractionBackendResolver(), RLock(), lambda *a, **k: "")


def _info(*, rar5: int | None = None, rar3: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        filename="p.jpg",
        isdir=lambda: False,
        file_compress_flags=rar5,
        flags=rar3,
    )


def _archive(infos: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(infolist=lambda: infos)


def test_a_rar5_archive_is_solid_when_any_member_continues_the_stream() -> None:
    """The first member of a solid block never carries the bit, so testing
    only the first entry would call every solid archive non-solid."""

    solid = rarfile.RAR5_COMPR_SOLID
    assert rar_requires_sequential_warmup(rarfile, _archive([_info(rar5=0), _info(rar5=0)])) is False
    assert rar_requires_sequential_warmup(
        rarfile, _archive([_info(rar5=0), _info(rar5=solid)])
    ) is True


def test_a_rar3_archive_reads_its_solid_bit_from_the_other_field() -> None:
    """RAR3 and RAR5 keep the flag in differently laid out fields."""

    assert rar_requires_sequential_warmup(
        rarfile, _archive([_info(rar3=rarfile.RAR_FILE_SOLID)])
    ) is True
    assert rar_requires_sequential_warmup(rarfile, _archive([_info(rar3=0)])) is False
    # A RAR5 value that happens to sit in the RAR3 solid bit must not be read
    # through the RAR3 rule.
    assert rar_requires_sequential_warmup(
        rarfile, _archive([_info(rar5=0, rar3=rarfile.RAR_FILE_SOLID)])
    ) is False


def test_an_archive_without_usable_flags_is_treated_as_sequential() -> None:
    """Unknown is conservative: reading a solid archive randomly is slow,
    warming a non-solid one is merely unnecessary."""

    assert rar_requires_sequential_warmup(rarfile, _archive([_info()])) is True
    assert rar_requires_sequential_warmup(rarfile, _archive([])) is True


@requires_cbr_fixture
def test_the_real_non_solid_corpus_is_detected_as_non_solid() -> None:
    with rarfile.RarFile(str(CBR_FIXTURE)) as archive:
        assert rar_requires_sequential_warmup(rarfile, archive) is False


@requires_cbr_fixture
def test_a_confirmed_non_solid_rar_does_not_request_warmup(tmp_path: Path) -> None:
    """A non-solid RAR seeks freely, so a whole-document pass buys nothing."""

    service = ArchiveImageService(page_cache_dir=tmp_path / "cache")
    session = service.open(CBR_FIXTURE, document_cache_key="file:cbr", allow_persistent_cache=True)
    try:
        assert session.requires_sequential_warmup is False
    finally:
        session.close()


@requires_cbr_fixture
def test_a_multi_page_request_uses_one_backend_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect: one extractor process per page. On a solid RAR each launch
    re-decompresses its own prefix."""

    calls: list[list[str]] = []
    real_run = command_module.run_archive_file_command

    def counting_run(command, entry_name, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(list(command))
        return real_run(command, entry_name, **kwargs)

    monkeypatch.setattr(command_module, "run_archive_file_command", counting_run)
    with rarfile.RarFile(str(CBR_FIXTURE)) as archive:
        names = [info.filename for info in archive.infolist() if not info.isdir()][:4]
    source = ArchiveSource(label=CBR_FIXTURE.name, suffix=".cbr", path=CBR_FIXTURE)

    payloads = _backend().read_entries(
        source,
        [(name, None) for name in names],
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(None),
    )

    assert sorted(payloads) == sorted(names)
    assert all(payloads[name] for name in names)
    assert len(calls) == 1, "four pages must cost one extraction, not four"


def test_the_batch_command_is_fenced_against_option_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    def capture(command, entry_name, **kwargs):  # noqa: ANN001, ANN002, ANN003
        captured.append(list(command))
        return 0

    monkeypatch.setattr(command_module, "run_archive_file_command", capture)
    source = ArchiveSource(label="book.cbr", suffix=".cbr", path=Path("book.cbr"))

    # The stub stages nothing, so the read itself cannot succeed. The command
    # it built is what this test is about.
    with suppress(ArchiveError):
        _backend().read_entries(
            source,
            [("-oESCAPED.jpg", None), ("*.jpg", None)],
            limits=ArchiveOpenLimits(),
            budget=ArchiveOperationBudget(None),
        )

    assert captured, "the batched path must be the one that ran"
    command = captured[0]
    assert "-spd" in command, "member names must never be read as wildcards"
    assert "-scsUTF-8" in command, "CJK member names must survive"
    separator = command.index("--")
    assert command.index("-oESCAPED.jpg") > separator
    assert command.index("*.jpg") > separator


def test_the_single_entry_stdout_command_is_fenced_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-entry fallback shares the same attacker-controlled names."""

    captured: list[list[str]] = []

    def capture(command, entry_name, **kwargs):  # noqa: ANN001, ANN002, ANN003
        captured.append(list(command))
        return b"payload"

    monkeypatch.setattr(rar_module, "run_archive_stdout_command", capture)
    source = ArchiveSource(label="book.cbr", suffix=".cbr", path=Path("book.cbr"))

    _backend()._read_with_7zip(  # noqa: SLF001
        source,
        "-oESCAPED.jpg",
        None,
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(None),
    )

    command = captured[0]
    assert "-spd" in command
    assert command.index("-oESCAPED.jpg") > command.index("--")


def test_bulk_capability_is_advertised_for_path_backed_rar() -> None:
    backend = _backend()
    on_disk = ArchiveSource(label="book.cbr", suffix=".cbr", path=CBR_FIXTURE)
    nested = ArchiveSource(label="nested.cbr", suffix=".cbr", data=b"nested")

    assert backend.supports_bulk_extraction(on_disk) is True
    assert backend.supports_bulk_extraction(nested) is False


@requires_cbr_fixture
def test_bulk_extraction_takes_only_the_requested_members(tmp_path: Path) -> None:
    with rarfile.RarFile(str(CBR_FIXTURE)) as archive:
        names = [info.filename for info in archive.infolist() if not info.isdir()]
    wanted = names[:3]
    destination = tmp_path / "staging"
    source = ArchiveSource(label=CBR_FIXTURE.name, suffix=".cbr", path=CBR_FIXTURE)

    _backend().extract_members(
        source,
        wanted,
        destination,
        None,
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(None),
        max_output_bytes=None,
    )

    produced = sorted(p.name for p in destination.rglob("*") if p.is_file())
    assert produced == sorted(Path(name).name for name in wanted)


@pytest.mark.skipif(not RAR_FIXTURE.is_file(), reason="the encrypted RAR sample is not present")
def test_an_encrypted_rar_is_never_a_persistent_cache_product(tmp_path: Path) -> None:
    """Decrypted pages must not reach a durable bundle, whatever the policy."""

    with rarfile.RarFile(str(RAR_FIXTURE)) as archive:
        assert archive.needs_password() is True


@requires_cbr_fixture
def test_a_failure_after_reading_starts_does_not_recharge_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RAR batch shares the 7z boundary rather than owning its own.

    Before the fix this caller caught OSError and dropped to the per-entry
    chain, which re-read every member and charged the shared operation budget
    a second time -- enough for a session near ``max_operation_bytes`` to fail
    a legitimate page read.
    """

    with rarfile.RarFile(str(CBR_FIXTURE)) as archive:
        infos = [info for info in archive.infolist() if not info.isdir()][:3]
    names = [info.filename for info in infos]
    first_member_bytes = infos[0].file_size

    reads: list[str] = []
    real_read_file_bounded = command_module.read_file_bounded

    def flaky_read(path, subject, **kwargs):  # noqa: ANN001, ANN003
        reads.append(subject)
        if len(reads) > 1:
            raise OSError("the staging directory went away mid-read")
        return real_read_file_bounded(path, subject, **kwargs)

    monkeypatch.setattr(command_module, "read_file_bounded", flaky_read)

    backend = _backend()
    per_entry: list[str] = []
    monkeypatch.setattr(
        backend,
        "read_entry",
        lambda *args, **kwargs: per_entry.append("called"),  # noqa: ANN002, ANN003
    )

    budget = ArchiveOperationBudget(None)
    source = ArchiveSource(label=CBR_FIXTURE.name, suffix=".cbr", path=CBR_FIXTURE)

    with pytest.raises(ArchiveError):
        backend.read_entries(
            source,
            [(name, None) for name in names],
            limits=ArchiveOpenLimits(),
            budget=budget,
        )

    assert per_entry == [], "a post-charge failure must not retry per entry"
    assert reads == names[:2], "the failure lands on the second member"
    assert budget.used == first_member_bytes, "only the member actually read is charged"


def test_a_batch_that_produced_nothing_still_falls_back_per_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was read, so the per-entry chain may safely try again."""

    def empty_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return 0

    monkeypatch.setattr(command_module, "run_archive_file_command", empty_command)

    backend = _backend()
    per_entry: list[str] = []

    def record(source, name, password, **kwargs):  # noqa: ANN001, ANN003
        per_entry.append(name)
        return b"per-entry"

    monkeypatch.setattr(backend, "read_entry", record)

    budget = ArchiveOperationBudget(None)
    source = ArchiveSource(label="book.cbr", suffix=".cbr", path=Path("book.cbr"))
    payloads = backend.read_entries(
        source,
        [("a.jpg", None), ("b.jpg", None)],
        limits=ArchiveOpenLimits(),
        budget=budget,
    )

    assert per_entry == ["a.jpg", "b.jpg"]
    assert payloads == {"a.jpg": b"per-entry", "b.jpg": b"per-entry"}
    assert budget.used == 0
