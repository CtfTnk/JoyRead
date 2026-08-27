"""Which decryptor reads an encrypted ZIP entry, and why it matters.

ZipCrypto and AES are not comparable in cost. AES is handed to a C backend and
decrypts at ~166 MB/s; ZipCrypto goes through ``zipfile._ZipDecrypter``, a
per-byte Python loop that holds the GIL, measured at ~2.6 MB/s. On a real
48-page book that is ~1.2 s per page against ~42 ms.

So ZipCrypto entries are decrypted by the 7-Zip helper and AES entries are not
-- and the AES half is not an oversight. Keeping AES in process is both as fast
and strictly safer, because 7-Zip accepts a password only as a command-line
argument.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pyzipper
import pytest

from joyread.core.archive.backends import ExtractionBackend
from joyread.core.archive.errors import ArchivePasswordRejected
from joyread.core.archive.formats import zip_backend as zip_module
from joyread.core.archive.formats.zip_backend import ZipArchiveBackend, uses_zipcrypto
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.records import ArchiveSource

PASSWORD = "secret"
_HELPER = (
    Path(__file__).resolve().parents[2]
    / "src/joyread/resources/extractors/7zip/darwin-arm64/7zz"
)
requires_helper = pytest.mark.skipif(
    not _HELPER.is_file(), reason="needs the bundled 7-Zip helper for this platform"
)


class _Resolver:
    def __init__(self, executable: str | None) -> None:
        self._executable = executable
        self.calls = 0

    def seven_zip(self):  # noqa: ANN201
        self.calls += 1
        if self._executable is None:
            return None
        return ExtractionBackend("7zz", self._executable, "test", supports_passwords=True)


def _encrypted_zip(tmp_path: Path, cipher: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = tmp_path / "001.txt"
    payload.write_bytes(b"page-bytes")
    archive = tmp_path / f"{cipher}.zip"
    subprocess.run(
        [str(_HELPER), "a", "-tzip", f"-mem={cipher}", f"-p{PASSWORD}",
         str(archive), str(payload)],
        check=True, capture_output=True,
    )
    return archive


def _backend(
    executable: str | None,
    *,
    resolver: _Resolver | None = None,
) -> ZipArchiveBackend:
    return ZipArchiveBackend(
        zipper_getter=lambda: pyzipper,
        bad_file_errors_getter=lambda: (pyzipper.zipfile.BadZipFile,),
        request_password=lambda *a, **k: PASSWORD,
        backend_resolver=resolver or _Resolver(executable),
    )


def _read(backend: ZipArchiveBackend, source: ArchiveSource) -> bytes:
    return backend.read_entries(
        source,
        [("001.txt", PASSWORD)],
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(1 << 30),
    )["001.txt"]


@requires_helper
def test_zipcrypto_and_aes_are_told_apart_from_the_central_directory(tmp_path: Path) -> None:
    """No subprocess is needed to choose: WinZip AES leaves a 0x9901 extra field."""

    for cipher, expected in (("ZipCrypto", True), ("AES256", False)):
        with pyzipper.AESZipFile(_encrypted_zip(tmp_path / cipher, cipher)) as archive:
            assert uses_zipcrypto(archive.infolist()[0]) is expected, cipher


def test_an_unencrypted_entry_is_never_zipcrypto(tmp_path: Path) -> None:
    archive_path = tmp_path / "plain.zip"
    with pyzipper.AESZipFile(archive_path, "w") as archive:
        archive.writestr("001.txt", b"page-bytes")
    with pyzipper.AESZipFile(archive_path) as archive:
        assert uses_zipcrypto(archive.infolist()[0]) is False


@requires_helper
def test_a_zipcrypto_entry_is_read_through_the_helper(tmp_path: Path, monkeypatch) -> None:
    archive = _encrypted_zip(tmp_path, "ZipCrypto")
    calls: list[list[str]] = []
    real = zip_module.run_archive_stdout_command
    monkeypatch.setattr(
        zip_module,
        "run_archive_stdout_command",
        lambda command, *a, **k: (calls.append(list(command)), real(command, *a, **k))[1],
    )

    payload = _read(_backend(str(_HELPER)), ArchiveSource(label=archive.name, suffix=".zip", path=archive))

    assert payload == b"page-bytes"
    assert len(calls) == 1, "a ZipCrypto entry must not be decrypted in process"
    assert "--" in calls[0], "the archive path must be separated from switches"


@requires_helper
def test_an_aes_entry_stays_in_process(tmp_path: Path, monkeypatch) -> None:
    """AES keeps the in-process guarantee: as fast, and the password never
    reaches a command line where `ps` can read it."""

    archive = _encrypted_zip(tmp_path, "AES256")
    calls: list[object] = []
    resolver = _Resolver(str(_HELPER))
    monkeypatch.setattr(
        zip_module, "run_archive_stdout_command",
        lambda *a, **k: calls.append(a) or b"",
    )

    payload = _read(
        _backend(str(_HELPER), resolver=resolver),
        ArchiveSource(label=archive.name, suffix=".zip", path=archive),
    )

    assert payload == b"page-bytes"
    assert calls == [], "an AES entry must not be handed to the helper"
    assert resolver.calls == 0, "AES must not even resolve an external helper"


@requires_helper
def test_helper_password_rejection_preserves_nested_archive_identity(tmp_path: Path) -> None:
    archive = _encrypted_zip(tmp_path, "ZipCrypto")
    source = ArchiveSource(
        label="outer.cbz::nested.zip",
        suffix=".zip",
        path=archive,
        spilled=True,
    )

    with pytest.raises(ArchivePasswordRejected) as exc_info:
        _backend(str(_HELPER)).read_entries(
            source,
            [("001.txt", "wrong")],
            limits=ArchiveOpenLimits(),
            budget=ArchiveOperationBudget(1 << 30),
        )

    assert exc_info.value.archive_path == source.label


@requires_helper
def test_a_source_without_a_path_falls_back_in_process(tmp_path: Path, monkeypatch) -> None:
    """The helper takes a path, so a bytes-only source cannot use it."""

    archive = _encrypted_zip(tmp_path, "ZipCrypto")
    calls: list[object] = []
    monkeypatch.setattr(
        zip_module, "run_archive_stdout_command",
        lambda *a, **k: calls.append(a) or b"",
    )

    payload = _read(
        _backend(str(_HELPER)),
        ArchiveSource(label="nested.zip", suffix=".zip", data=archive.read_bytes()),
    )

    assert payload == b"page-bytes"
    assert calls == []


@requires_helper
def test_no_helper_available_still_reads(tmp_path: Path) -> None:
    """Slow beats broken: without an executable the Python path still serves."""

    archive = _encrypted_zip(tmp_path, "ZipCrypto")
    payload = _read(_backend(None), ArchiveSource(label=archive.name, suffix=".zip", path=archive))
    assert payload == b"page-bytes"
