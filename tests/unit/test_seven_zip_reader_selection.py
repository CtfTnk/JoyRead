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
from joyread.core.archive.formats import seven_zip_command as command_module
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

    monkeypatch.setattr(command_module, "run_archive_file_command", failing_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", rejecting_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", limited_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", staging_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", capturing_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", partial_command)

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

    monkeypatch.setattr(command_module, "run_archive_file_command", staging_command)

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


def _real_backend():
    from joyread.core.archive.backends import ExtractionBackendResolver
    return SevenZipArchiveBackend(lambda: None, lambda **_k: "", ExtractionBackendResolver())


def _make_archive(tmp_path: Path, names: list[str]) -> Path:
    """Build a real 7z with the bundled executable."""
    import subprocess
    from joyread.core.archive.backends import ExtractionBackendResolver
    backend = ExtractionBackendResolver().seven_zip()
    assert backend is not None
    src = tmp_path / "src"
    src.mkdir()
    for name in names:
        (src / name).write_bytes(name.encode("utf-8") * 64)
    archive = tmp_path / "book.7z"
    subprocess.run(
        [backend.executable, "a", "-bso0", "-bsp0", str(archive), "."],
        cwd=src, check=True, capture_output=True,
    )
    return archive


def test_extract_members_takes_only_the_requested_pages(tmp_path: Path) -> None:
    """Non-page files must never reach staging or the cache."""

    archive = _make_archive(tmp_path, ["p1.jpg", "p2.jpg", "thumbs.db", "nested.7z"])
    out = tmp_path / "out"
    _real_backend().extract_members(
        ArchiveSource(label="book.7z", suffix=".7z", path=archive),
        ["p1.jpg", "p2.jpg"],
        out,
        None,
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(maximum=None),
        max_output_bytes=None,
    )

    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["p1.jpg", "p2.jpg"]


def test_extract_members_handles_cjk_and_switch_like_names(tmp_path: Path) -> None:
    """Names travel in a listfile, so they cannot be read as switches."""

    names = ["普通页面 001.jpg", "-oTRICKY.jpg", "plain.jpg"]
    archive = _make_archive(tmp_path, names)
    out = tmp_path / "out"
    _real_backend().extract_members(
        ArchiveSource(label="book.7z", suffix=".7z", path=archive),
        names,
        out,
        None,
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(maximum=None),
        max_output_bytes=None,
    )

    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == sorted(names)


def test_a_member_name_with_a_line_break_is_refused(tmp_path: Path) -> None:
    """A listfile is newline-delimited; such a name would extract the wrong files."""

    with pytest.raises(command_module.MemberNameNotRepresentable):
        command_module.build_listfile_text(["ok.jpg", "bad\nname.jpg"])
    with pytest.raises(command_module.MemberNameNotRepresentable):
        command_module.build_listfile_text(["bad\rname.jpg"])

    assert command_module.build_listfile_text(["a.jpg", "b.jpg"]) == "a.jpg\nb.jpg\n"


def test_the_aggregate_cap_is_separate_from_the_per_member_cap(tmp_path: Path, monkeypatch) -> None:
    """1 GiB per member is the wrong ceiling for a whole book."""

    seen: dict[str, object] = {}

    def capture(command, entry_name, **kwargs):  # noqa: ANN001, ANN002, ANN003
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(command_module, "run_archive_file_command", capture)
    archive = tmp_path / "book.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c")

    backend = SevenZipArchiveBackend(
        lambda: None, lambda **_k: "", backend_resolver=_Resolver(_Executable())
    )
    backend.extract_members(
        ArchiveSource(label="book.7z", suffix=".7z", path=archive),
        ["a.jpg"],
        tmp_path / "out",
        None,
        limits=ArchiveOpenLimits(max_extracted_item_bytes=1024),
        budget=ArchiveOperationBudget(maximum=None),
        max_output_bytes=99_000,
    )

    assert seen["max_output_bytes"] == 99_000
    assert seen["timeout_seconds"] is None, "background conversion uses stall, not wall clock"


def test_a_multi_member_read_uses_the_operation_cap_for_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several legal members may legitimately exceed one member's ceiling."""

    staged = {"a.jpg": b"a" * 700, "b.jpg": b"b" * 700}
    seen: dict[str, object] = {}

    def staging_command(_command, _entry_name, **kwargs):  # noqa: ANN001, ANN003
        seen.update(kwargs)
        directory = kwargs["output_directory"]
        for name, payload in staged.items():
            (directory / name).write_bytes(payload)
        return sum(len(payload) for payload in staged.values())

    monkeypatch.setattr(command_module, "run_archive_file_command", staging_command)
    budget = ArchiveOperationBudget(maximum=4096)
    backend = SevenZipArchiveBackend(
        lambda: None,
        lambda **_kwargs: "",
        backend_resolver=_Resolver(_Executable()),
    )

    payloads = backend.read_entries(
        _source(tmp_path),
        [(name, None) for name in staged],
        limits=ArchiveOpenLimits(
            max_extracted_item_bytes=1024,
            max_operation_bytes=4096,
        ),
        budget=budget,
    )

    assert payloads == staged
    assert seen["max_output_bytes"] == 4096
    assert budget.used == 1400


def test_cancellation_stops_extraction_without_a_fallback_outcome(tmp_path: Path) -> None:
    from joyread.core.archive.errors import ArchiveCancelled
    from joyread.core.archive.formats.common import run_archive_file_command

    staging = tmp_path / "out"
    staging.mkdir()
    cancelled = {"value": False}

    def is_cancelled() -> bool:
        return cancelled["value"]

    import threading
    threading.Timer(0.3, lambda: cancelled.__setitem__("value", True)).start()

    with pytest.raises(ArchiveCancelled):
        run_archive_file_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "page-001.jpg",
            password=None,
            timeout_seconds=None,
            output_directory=staging,
            max_output_bytes=None,
            budget=ArchiveOperationBudget(maximum=None),
            stall_seconds=None,
            is_cancelled=is_cancelled,
        )


def test_a_failure_after_reading_starts_is_terminal(tmp_path: Path, monkeypatch) -> None:
    """Falling back once the budget is charged would bill the same bytes twice.

    ``read_members_via_executable`` owns the boundary: it may return ``None``
    only while nothing has been read. After that, a failure has to propagate,
    or py7zr re-reads the same members and the shared operation budget
    double-counts them into a limit the real workload never reached.
    """

    staged = {"page-001.jpg": b"x" * 2048, "page-002.jpg": b"y" * 2048}

    def staging_command(*_args, **kwargs):  # noqa: ANN002, ANN003
        directory = kwargs["output_directory"]
        for name, payload in staged.items():
            (directory / name).write_bytes(payload)
        return sum(len(value) for value in staged.values())

    monkeypatch.setattr(command_module, "run_archive_file_command", staging_command)

    reads: list[str] = []
    real_read_file_bounded = command_module.read_file_bounded

    def flaky_read(path, subject, **kwargs):  # noqa: ANN001, ANN003
        reads.append(subject)
        if len(reads) > 1:
            raise OSError("the staging directory went away mid-read")
        return real_read_file_bounded(path, subject, **kwargs)

    monkeypatch.setattr(command_module, "read_file_bounded", flaky_read)

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            raise AssertionError("a post-charge failure must not fall back to py7zr")

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

    assert budget.used == 2048, "only the members actually read may be charged"


def test_a_failure_before_reading_starts_still_falls_back(
    tmp_path: Path, monkeypatch
) -> None:
    """The other side of the boundary: nothing charged, so a retry is safe."""

    def empty_command(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return 0

    monkeypatch.setattr(command_module, "run_archive_file_command", empty_command)

    served: list[str] = []

    class _Product:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def take_bytes(self) -> bytes:
            return self._payload

    class _Archive:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args) -> bool:  # noqa: ANN002
            return False

        def extract(self, targets, factory):  # noqa: ANN001
            for name in targets:
                served.append(name)
                factory.products[name] = _Product(b"from-py7zr")

    class _Py7zr:
        PasswordRequired = type("PasswordRequired", (Exception,), {})
        DecompressionError = type("DecompressionError", (Exception,), {})
        Bad7zFile = type("Bad7zFile", (Exception,), {})

        @staticmethod
        def SevenZipFile(*_args, **_kwargs):  # noqa: ANN002, ANN003, N802
            return _Archive()

    budget = ArchiveOperationBudget(maximum=None)
    backend = SevenZipArchiveBackend(
        lambda: _Py7zr(), lambda **_kwargs: "", backend_resolver=_Resolver(_Executable())
    )
    payloads = backend.read_entries(
        _source(tmp_path),
        [("page-001.jpg", None)],
        limits=ArchiveOpenLimits(),
        budget=budget,
    )

    assert served == ["page-001.jpg"]
    assert payloads["page-001.jpg"] == b"from-py7zr"
    assert budget.used == 0, "the abandoned executable read must charge nothing"


def test_a_member_stored_without_a_read_bit_is_still_readable(tmp_path: Path) -> None:
    """7-Zip applies the container's stored mode to the file it stages.

    A member written with no owner-read bit -- which ``py7zr.writestr``
    produces, and which nothing stops a real archive from carrying -- therefore
    lands in our own temporary directory as a file we cannot open. Before the
    fix this failed the page outright, because narrowing the fallback made a
    post-extraction ``OSError`` terminal by design. The mode belongs to the
    original file, not to our throwaway copy, so it is corrected instead.
    """

    staging = tmp_path / "staging"
    staging.mkdir()
    unreadable = staging / "page-001.jpg"
    unreadable.write_bytes(b"payload")
    unreadable.chmod(0o000)

    staged = command_module.resolve_staged_targets(staging, ("page-001.jpg",))

    assert staged is not None, "an unreadable staged file must not look like a missing one"
    budget = ArchiveOperationBudget(maximum=None)
    payloads = command_module.read_staged_payloads(staged, ArchiveOpenLimits(), budget)
    assert payloads == {"page-001.jpg": b"payload"}
    assert budget.used == len(b"payload")


def test_a_staged_file_that_cannot_be_made_readable_falls_back_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair happens before any byte is charged, so failing it is a capability
    failure the caller may retry through another backend -- not a terminal one."""

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "page-001.jpg").write_bytes(b"payload")

    monkeypatch.setattr(command_module.os, "access", lambda *_args, **_kwargs: False)

    assert command_module.resolve_staged_targets(staging, ("page-001.jpg",)) is None
