from __future__ import annotations

import sys
from pathlib import Path

import pytest

from joyread.core.archive.errors import (
    ArchiveDependencyMissing,
    ArchivePasswordRejected,
    ArchiveResourceLimitError,
    ArchiveReadError,
)
from joyread.core.archive.formats import seven_zip_backend as backend_module
from joyread.core.archive.formats.seven_zip_backend import SevenZipArchiveBackend
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveSource


class _Resolver:
    """Stands in for ExtractionBackendResolver."""

    def __init__(self, backend: object | None) -> None:
        self._backend = backend
        self.lookups = 0

    def seven_zip(self):  # noqa: ANN201
        self.lookups += 1
        return self._backend


class _Executable:
    def __init__(self, executable: str = "/fake/7zz") -> None:
        self.executable = executable


def _source(tmp_path: Path) -> ArchiveSource:
    archive = tmp_path / "book.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c")
    return ArchiveSource(label="book.7z", suffix=".7z", path=archive)


def _read(backend: SevenZipArchiveBackend, source: ArchiveSource):  # noqa: ANN202
    return backend.read_entries(
        source,
        [("page-001.jpg", None)],
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(maximum=None),
    )


def test_py7zr_serves_the_read_when_no_executable_resolves(tmp_path: Path) -> None:
    """The bundled binary is the fast path, not a hard requirement."""

    used: list[str] = []

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            used.append("py7zr")
            raise ArchiveReadError("py7zr reached")

    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(),
        lambda **_kwargs: "",
        backend_resolver=_Resolver(None),
    )

    with pytest.raises(ArchiveReadError):
        _read(backend, _source(tmp_path))

    assert used == ["py7zr"]


def test_a_missing_executable_is_only_looked_up_once(tmp_path: Path) -> None:
    resolver = _Resolver(None)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            raise ArchiveReadError("py7zr reached")

    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(), lambda **_kwargs: "", backend_resolver=_Resolver(None)
    )
    backend._backend_resolver = resolver  # noqa: SLF001
    source = _source(tmp_path)

    for _ in range(3):
        with pytest.raises(ArchiveReadError):
            _read(backend, source)

    assert resolver.lookups == 1


def test_an_executable_failure_falls_back_to_py7zr(tmp_path: Path, monkeypatch) -> None:
    """Retrying the same command over the same bytes would fail identically."""

    used: list[str] = []

    def failing_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        used.append("7zip")
        raise ArchiveReadError("7zz exited non-zero")

    monkeypatch.setattr(backend_module, "run_archive_file_command", failing_command)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            used.append("py7zr")
            raise ArchiveReadError("py7zr reached")

    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(),
        lambda **_kwargs: "",
        backend_resolver=_Resolver(_Executable()),
    )

    with pytest.raises(ArchiveReadError):
        _read(backend, _source(tmp_path))

    assert used == ["7zip", "py7zr"]


def test_a_rejected_password_is_raised_rather_than_retried(tmp_path: Path, monkeypatch) -> None:
    """py7zr would reject the same password; surface it instead of stalling."""

    used: list[str] = []

    def rejecting_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        used.append("7zip")
        raise ArchivePasswordRejected("bad password", archive_path="book.7z")

    monkeypatch.setattr(backend_module, "run_archive_file_command", rejecting_command)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            used.append("py7zr")
            raise AssertionError("a rejected password must not fall back")

    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(),
        lambda **_kwargs: "",
        backend_resolver=_Resolver(_Executable()),
    )

    with pytest.raises(ArchivePasswordRejected):
        _read(backend, _source(tmp_path))

    assert used == ["7zip"]


def test_neither_reader_available_reports_both_options(tmp_path: Path) -> None:
    backend = SevenZipArchiveBackend(
        lambda: None, lambda **_kwargs: "", backend_resolver=_Resolver(None)
    )

    with pytest.raises(ArchiveDependencyMissing) as excinfo:
        _read(backend, _source(tmp_path))

    assert "py7zr" in str(excinfo.value)


def test_a_stalled_extraction_is_abandoned_but_a_progressing_one_is_not(tmp_path: Path) -> None:
    """Progress, not elapsed time, decides whether a backend is wedged."""

    from joyread.core.archive.formats.common import run_archive_file_command

    staging = tmp_path / "out"
    staging.mkdir()
    budget = ArchiveOperationBudget(maximum=None)

    # A silent process that writes nothing at all is stalled from the start.
    with pytest.raises(ArchiveResourceLimitError) as excinfo:
        run_archive_file_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "page-001.jpg",
            password=None,
            timeout_seconds=60,
            output_directory=staging,
            max_output_bytes=None,
            budget=budget,
            stall_seconds=0.3,
        )
    assert excinfo.value.limit == "external_command_stall_seconds"

    # A process that keeps writing outlives the same stall window.
    script = (
        "import pathlib,time\n"
        "d=pathlib.Path(%r)\n"
        "for i in range(6):\n"
        "    (d/f'p{i}.bin').write_bytes(b'x'*4096)\n"
        "    time.sleep(0.1)\n" % str(staging)
    )
    run_archive_file_command(
        [sys.executable, "-c", script],
        "page-001.jpg",
        password=None,
        timeout_seconds=60,
        output_directory=staging,
        max_output_bytes=None,
        budget=budget,
        stall_seconds=0.3,
    )
    assert len(list(staging.glob("p*.bin"))) == 6


def test_a_resource_limit_is_not_retried_through_the_unguarded_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """py7zr has no timeout or stall guard, so a refusal must stay a refusal."""

    def limited_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ArchiveResourceLimitError("external_command_stall_seconds", subject="page-001.jpg")

    monkeypatch.setattr(backend_module, "run_archive_file_command", limited_command)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            raise AssertionError("a resource limit must not fall back to py7zr")

    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(), lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )

    with pytest.raises(ArchiveResourceLimitError):
        _read(backend, _source(tmp_path))


def test_executable_reads_charge_the_shared_operation_budget(
    tmp_path: Path, monkeypatch
) -> None:
    """The executable writes to disk, bypassing py7zr's budgeted writer."""

    staged = {"page-001.jpg": b"x" * 2048}

    def staging_command(*_args, **kwargs):  # noqa: ANN002, ANN003
        directory = kwargs["output_directory"]
        for name, payload in staged.items():
            (directory / name).write_bytes(payload)
        return sum(len(v) for v in staged.values())

    monkeypatch.setattr(backend_module, "run_archive_file_command", staging_command)

    budget = ArchiveOperationBudget(maximum=None)
    backend = SevenZipArchiveBackend(
        lambda: None, lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )
    payloads = backend.read_entries(
        _source(tmp_path),
        [("page-001.jpg", None)],
        limits=ArchiveOpenLimits(),
        budget=budget,
    )

    assert payloads["page-001.jpg"] == staged["page-001.jpg"]
    assert budget.used == 2048


def test_member_names_cannot_be_read_as_seven_zip_switches(
    tmp_path: Path, monkeypatch
) -> None:
    """A member called "-oESCAPED" would otherwise become a second -o switch."""

    captured: list[list[str]] = []

    def capturing_command(command, *_args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        captured.append(list(command))
        (kwargs["output_directory"] / "-oESCAPED").write_bytes(b"payload")
        return 7

    monkeypatch.setattr(backend_module, "run_archive_file_command", capturing_command)

    backend = SevenZipArchiveBackend(
        lambda: None, lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )
    backend.read_entries(
        _source(tmp_path),
        [("-oESCAPED", None)],
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(maximum=None),
    )

    command = captured[0]
    assert "--" in command, "positional arguments must be fenced off from switches"
    assert command.index("--") < command.index("-oESCAPED")
    assert "-spd" in command, "member names must not be matched as wildcards"


def test_backend_output_counts_as_liveness_without_any_staged_bytes(
    tmp_path: Path,
) -> None:
    """A solid archive writes nothing while decompressing earlier members."""

    from joyread.core.archive.formats.common import run_archive_file_command

    staging = tmp_path / "quiet"
    staging.mkdir()
    # Chatters on stderr but never writes a file, like 7zz with -bsp2 while it
    # decompresses its way toward a late member.
    script = (
        "import sys,time\n"
        "for _ in range(8):\n"
        "    sys.stderr.write('  50%\\r'); sys.stderr.flush(); time.sleep(0.1)\n"
    )
    run_archive_file_command(
        [sys.executable, "-c", script],
        "page-001.jpg",
        password=None,
        timeout_seconds=60,
        output_directory=staging,
        max_output_bytes=None,
        budget=ArchiveOperationBudget(maximum=None),
        stall_seconds=0.3,
    )
    assert not list(staging.iterdir())


def test_a_partial_extraction_charges_nothing_before_falling_back(
    tmp_path: Path, monkeypatch
) -> None:
    """7-Zip exits 0 even when a requested member is missing.

    The budget must stay untouched on that path, or py7zr will charge the same
    bytes a second time when it retries the whole batch.
    """

    def partial_command(*_args, **kwargs):  # noqa: ANN002, ANN003
        # Produces the first entry but not the second.
        (kwargs["output_directory"] / "page-001.jpg").write_bytes(b"x" * 4096)
        return 4096

    monkeypatch.setattr(backend_module, "run_archive_file_command", partial_command)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            raise ArchiveReadError("py7zr reached")

    budget = ArchiveOperationBudget(maximum=None)
    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(), lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )

    with pytest.raises(ArchiveReadError):
        backend.read_entries(
            _source(tmp_path),
            [("page-001.jpg", None), ("page-002.jpg", None)],
            limits=ArchiveOpenLimits(),
            budget=budget,
        )

    assert budget.used == 0, "a fallback must not leave the batch half-charged"


def test_an_oversized_member_is_rejected_before_it_is_read(
    tmp_path: Path, monkeypatch
) -> None:
    """The size guard must run on stat, not after allocating the whole file."""

    def staging_command(*_args, **kwargs):  # noqa: ANN002, ANN003
        (kwargs["output_directory"] / "page-001.jpg").write_bytes(b"x" * 8192)
        return 8192

    monkeypatch.setattr(backend_module, "run_archive_file_command", staging_command)

    reads: list[str] = []
    real_read_bytes = Path.read_bytes

    def tracking_read_bytes(self):  # noqa: ANN001, ANN202
        reads.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", tracking_read_bytes)

    backend = SevenZipArchiveBackend(
        lambda: None, lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )

    with pytest.raises(ArchiveResourceLimitError):
        backend.read_entries(
            _source(tmp_path),
            [("page-001.jpg", None)],
            limits=ArchiveOpenLimits(max_extracted_item_bytes=1024),
            budget=ArchiveOperationBudget(maximum=None),
        )

    assert "page-001.jpg" not in reads, "the member was allocated before being rejected"
