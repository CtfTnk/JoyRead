"""ZIP names stored in a legacy code page read the way their authors wrote them.

A name without the UTF-8 flag is whatever code page the author's system used:
Shift-JIS on Japanese Windows, GBK on Chinese Windows. ``zipfile`` reads all
of them as cp437, so a Japanese chapter folder became box-drawing mojibake in
the Contents and sorted by its mojibake. On macOS and Linux, a ZipCrypto book
with such names did not open at all, because the garbled name was handed to
7-Zip, which knows the entry by a different name again.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import struct
import subprocess
import zipfile
import zlib

from PIL import Image
import pyzipper
import pytest

from joyread.core.archive import ArchiveImageService
from joyread.core.archive.backends import ExtractionBackendResolver
from joyread.core.archive.formats import zip_backend as zip_module
from joyread.core.archive.formats.zip_backend import ZipArchiveBackend
from joyread.core.archive.formats.zip_names import ZipMemberIndex
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget
from joyread.core.archive.models import ArchiveCachePolicy
from joyread.core.archive.name_encoding import choose_legacy_codec
from joyread.core.archive.records import ArchiveSource
from joyread.core.services.archive_cache_lease import ArchiveCacheLease, ArchiveCacheScope
from joyread.core.services.archive_extraction_pool import ArchiveExtractionPool

PASSWORD = "secret"


def _helper() -> Path | None:
    backend = ExtractionBackendResolver().seven_zip()
    if backend is None or not backend.source.startswith("bundled:"):
        return None
    return Path(backend.executable)


_HELPER = _helper()
requires_helper = pytest.mark.skipif(_HELPER is None, reason="needs the bundled 7-Zip helper")


def _png(width: int = 40) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, 30), "#ffffff").save(buffer, format="PNG")
    return buffer.getvalue()


def _placeholder(index: int, raw: bytes) -> bytes:
    return (b"N%03d" % index).ljust(len(raw), b"_")


def _store_raw_names(data: bytes, placeholders: list[bytes], raw_names: list[bytes]) -> bytes:
    """Swap ASCII placeholder names for legacy bytes of the same length.

    No Python writer emits a legacy name: ``zipfile`` flags anything non-ASCII
    as UTF-8. Same-length names leave every offset in the file valid, and the
    name is covered by no CRC, so this is exactly what a Windows tool writes.
    """

    for placeholder, raw in zip(placeholders, raw_names):
        assert data.count(placeholder) == 2, "one local header and one directory entry"
        data = data.replace(placeholder, raw)
    return data


def _legacy_zip(path: Path, raw_names: list[bytes], *, extras: list[bytes] | None = None) -> Path:
    placeholders = [_placeholder(index, raw) for index, raw in enumerate(raw_names)]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, placeholder in enumerate(placeholders):
            info = zipfile.ZipInfo(placeholder.decode("ascii"))
            if extras is not None:
                info.extra = extras[index]
            archive.writestr(info, _png(40 + index))
    path.write_bytes(_store_raw_names(buffer.getvalue(), placeholders, raw_names))
    return path


def _legacy_aes_zip(path: Path, raw_names: list[bytes]) -> Path:
    placeholders = [_placeholder(index, raw) for index, raw in enumerate(raw_names)]
    buffer = BytesIO()
    with pyzipper.AESZipFile(buffer, "w", compression=8, encryption=pyzipper.WZ_AES) as archive:
        archive.setpassword(PASSWORD.encode("utf-8"))
        for index, placeholder in enumerate(placeholders):
            archive.writestr(placeholder.decode("ascii"), _png(40 + index))
    path.write_bytes(_store_raw_names(buffer.getvalue(), placeholders, raw_names))
    return path


def _legacy_zipcrypto_zip(tmp_path: Path, raw_name: bytes) -> Path:
    assert _HELPER is not None
    placeholder = _placeholder(0, raw_name)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / placeholder.decode("ascii")).write_bytes(_png())
    archive = tmp_path / "zipcrypto.zip"
    subprocess.run(
        [str(_HELPER), "a", "-tzip", "-mem=ZipCrypto", f"-p{PASSWORD}", str(archive),
         placeholder.decode("ascii")],
        check=True, capture_output=True, cwd=staging,
    )
    archive.write_bytes(_store_raw_names(archive.read_bytes(), [placeholder], [raw_name]))
    return archive


def _backend() -> ZipArchiveBackend:
    return ZipArchiveBackend(
        zipper_getter=lambda: pyzipper,
        bad_file_errors_getter=lambda: (pyzipper.zipfile.BadZipFile,),
        request_password=lambda *a, **k: PASSWORD,
        backend_resolver=ExtractionBackendResolver(),
    )


def _source(path: Path) -> ArchiveSource:
    return ArchiveSource(label=path.name, suffix=".zip", path=path)


def _read(path: Path, name: str, password: str | None = None) -> bytes:
    return _backend().read_entries(
        _source(path),
        [(name, password)],
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(1 << 30),
    )[name]


# ---------------------------------------------------------------------------
# Choosing the code page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "encoding"),
    [
        (["ワンピース 第1巻/001.jpg", "ワンピース 第1巻/表紙.jpg"], "cp932"),
        (["鬼滅の刃 第01話/001.jpg"], "cp932"),
        # 0x5C, a backslash, is the second byte of ソ and 表.
        (["表紙ソ.jpg"], "cp932"),
        (["海贼王 第1卷/001.jpg", "海贼王 第1卷/封面.jpg"], "gbk"),
        (["第01话/001.jpg"], "gbk"),
        (["鏈鋸人 第5卷/001.jpg", "鏈鋸人 第5卷/後記.jpg"], "cp950"),
        # macOS and Linux tools write UTF-8 without ever setting the flag.
        (["進撃の巨人/001.jpg"], "utf-8"),
        (["Astérix/001.jpg"], "utf-8"),
        # Windows Explorer in Western Europe writes cp850; some tools cp1252.
        (["Øresund/001.jpg"], "cp850"),
        (["Café/001.jpg", "Pokémon/002.jpg"], "cp1252"),
    ],
)
def test_names_read_with_the_code_page_they_were_written_in(names: list[str], encoding: str) -> None:
    raw_names = [name.encode(encoding) for name in names]

    codec = choose_legacy_codec(raw_names)

    assert [raw.decode(codec) for raw in raw_names] == names


@pytest.mark.parametrize(
    "name",
    ["Bücher", "Kapitel Übung", "Größe", "Señor", "naïve", "Crème brûlée", "Mädchen", "Ça va"],
)
def test_a_western_name_already_shown_correctly_keeps_its_reading(name: str) -> None:
    """cp437 is what JoyRead always used. Every name it already showed right
    must survive the competition with the CJK readings."""

    raw = f"{name}/001.jpg".encode("cp437")

    assert raw.decode(choose_legacy_codec([raw])) == f"{name}/001.jpg"


def test_without_undeclared_non_ascii_names_the_zip_default_stands() -> None:
    assert choose_legacy_codec([]) == "cp437"
    assert choose_legacy_codec([b"Volume 01/001.jpg"]) == "cp437"


def test_short_names_are_decided_by_the_whole_archive() -> None:
    """One codec reads every name, so a two-character folder is carried by
    the longer names around it."""

    names = ["帯.jpg", "表紙.jpg", "第1話 始まりの日/001.jpg"]
    raw_names = [name.encode("cp932") for name in names]

    codec = choose_legacy_codec(raw_names)

    assert [raw.decode(codec) for raw in raw_names] == names


# ---------------------------------------------------------------------------
# The ZIP name index
# ---------------------------------------------------------------------------


def test_legacy_names_list_and_read_by_their_decoded_names(tmp_path: Path) -> None:
    names = ["ワンピース 第1巻/001.png", "ワンピース 第1巻/表紙ソ.png"]
    archive = _legacy_zip(tmp_path / "book.zip", [name.encode("cp932") for name in names])

    probe = _backend().probe_entries(_source(archive))

    assert [entry.name for entry in probe.entries] == names
    assert _read(archive, names[1]).startswith(b"\x89PNG")


def test_flagged_and_ascii_names_are_exactly_what_zipfile_reports(tmp_path: Path) -> None:
    archive = tmp_path / "book.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("第1話/001.png", _png())
        handle.writestr("Volume 01/002.png", _png())
    with pyzipper.AESZipFile(archive) as handle:
        infos = handle.infolist()

    index = ZipMemberIndex(infos)

    assert [member.name for member in index.files()] == [info.filename for info in infos]
    assert index.extractor_addressable


def test_a_unicode_path_extra_field_supplies_the_name(tmp_path: Path) -> None:
    stored = "第01話/001.png".encode("cp932")
    unicode_name = "第01話/001.png".encode("utf-8")
    extra = struct.pack("<HHBI", 0x7075, 5 + len(unicode_name), 1, zlib.crc32(stored))
    archive = _legacy_zip(tmp_path / "book.zip", [stored], extras=[extra + unicode_name])

    assert [entry.name for entry in _backend().probe_entries(_source(archive)).entries] == [
        "第01話/001.png"
    ]


def test_a_unicode_path_extra_field_for_another_name_is_ignored(tmp_path: Path) -> None:
    """The field carries a CRC of the stored name so that a tool which renamed
    the entry without updating the field cannot leave a stale name behind."""

    stored = "第01話/001.png".encode("cp932")
    stale = "旧名/001.png".encode("utf-8")
    extra = struct.pack("<HHBI", 0x7075, 5 + len(stale), 1, zlib.crc32(b"something else"))
    archive = _legacy_zip(tmp_path / "book.zip", [stored], extras=[extra + stale])

    assert [entry.name for entry in _backend().probe_entries(_source(archive)).entries] == [
        "第01話/001.png"
    ]


def test_a_decoded_name_is_cut_at_nul_like_zipfile_cuts_its_own(tmp_path: Path) -> None:
    archive = _legacy_zip(tmp_path / "book.zip", ["表紙.png".encode("cp932") + b"\0.exe"])

    assert [entry.name for entry in _backend().probe_entries(_source(archive)).entries] == ["表紙.png"]


def test_only_names_stored_as_utf8_or_ascii_count_as_addressable(tmp_path: Path) -> None:
    legacy = _legacy_zip(tmp_path / "legacy.zip", ["第01話/001.png".encode("cp932")])
    with pyzipper.AESZipFile(legacy) as handle:
        assert not ZipMemberIndex(handle.infolist()).extractor_addressable


# ---------------------------------------------------------------------------
# Encrypted books
# ---------------------------------------------------------------------------


@requires_helper
def test_a_zipcrypto_page_with_a_legacy_name_opens(tmp_path: Path, monkeypatch) -> None:
    """Before this, the read failed with "returned no data": 7-Zip was asked
    for the cp437 name and knows the entry by its own escaped one. The page
    is now decrypted in process, the one reader that agrees on the name."""

    archive = _legacy_zipcrypto_zip(tmp_path, "表紙ソ.png".encode("cp932"))
    calls: list[object] = []
    monkeypatch.setattr(
        zip_module, "run_archive_stdout_command", lambda *a, **k: calls.append(a) or b""
    )

    payload = _read(archive, "表紙ソ.png", PASSWORD)

    assert payload.startswith(b"\x89PNG")
    assert calls == [], "the helper cannot address this name"


@requires_helper
def test_a_zipcrypto_book_read_in_process_serializes_its_reads(tmp_path: Path) -> None:
    """In-process ZipCrypto is the GIL-bound loop, where a second reader only
    contends with the first."""

    from joyread.core.archive.scanner import ArchiveScanContext

    archive = _legacy_zipcrypto_zip(tmp_path, "表紙ソ.png".encode("cp932"))
    context = ArchiveScanContext(
        password_provider=lambda *a, **k: PASSWORD,
        password_policy=None,
        skipped_archives=set(),
        limits=ArchiveOpenLimits(),
        budget=ArchiveOperationBudget(1 << 30),
    )

    listing = _backend().list_entries(_source(archive), context)

    assert listing.supports_concurrent_reads is False
    assert listing.extractor_addressable is False


def _open_encrypted(archive: Path, pool: ArchiveExtractionPool, key: str):  # noqa: ANN202
    lease = ArchiveCacheLease(pool, key, ArchiveCacheScope.PERSISTENT)
    session = ArchiveImageService().open(
        archive,
        password_provider=lambda *_a, **_k: PASSWORD,
        cache_lease=lease,
    )
    return session, lease


@requires_helper
def test_an_encrypted_book_with_legacy_names_is_never_bulk_converted(tmp_path: Path) -> None:
    """Bulk conversion asks 7-Zip for the pages by name and would find none of
    them. Without it there is also no warmup: an encrypted book warms only
    through an out-of-process pass. The same book with ASCII names still
    converts, so the difference is the names alone."""

    pool = ArchiveExtractionPool(tmp_path / "pool", 64 * 1024 * 1024)
    folder = "第01話".encode("cp932")
    legacy = _legacy_aes_zip(
        tmp_path / "legacy.cbz", [folder + b"/001.png", folder + b"/002.png"]
    )
    ascii_named = _legacy_aes_zip(tmp_path / "ascii.cbz", [b"Ch01/001.png", b"Ch01/002.png"])

    session, lease = _open_encrypted(legacy, pool, "file:legacy")
    control, control_lease = _open_encrypted(ascii_named, pool, "file:ascii")
    try:
        assert session.cache_plan.policy is not ArchiveCachePolicy.BULK_CONVERT
        assert not session.requires_sequential_warmup
        assert session.get_image(1) is not None
        assert control.cache_plan.policy is ArchiveCachePolicy.BULK_CONVERT
    finally:
        session.close()
        lease.close()
        control.close()
        control_lease.close()


def test_the_contents_show_decoded_folder_names(tmp_path: Path) -> None:
    names = ["第1話 始まりの日/001.png", "第2話 再会/001.png"]
    archive = _legacy_zip(tmp_path / "book.cbz", [name.encode("cp932") for name in names])

    session = ArchiveImageService().open(archive)
    try:
        assert [entry.label for entry in session.contents] == ["第1話 始まりの日", "第2話 再会"]
    finally:
        session.close()


def test_import_repackages_a_nested_legacy_book_under_its_decoded_names(tmp_path: Path) -> None:
    """A nested book is converted once, at import, and the canonical copy
    keeps whatever names the reader saw. Decoding later cannot repair it."""

    inner = _legacy_zip(
        tmp_path / "inner.cbz",
        ["第1話/001.png".encode("cp932"), "第2話/001.png".encode("cp932")],
    )
    outer = tmp_path / "outer.cbz"
    with zipfile.ZipFile(outer, "w") as handle:
        handle.writestr("Vol01.cbz", inner.read_bytes())
    destination = tmp_path / "canonical.cbz"

    ArchiveImageService().convert_to_canonical(outer, destination)

    with zipfile.ZipFile(destination) as handle:
        assert handle.namelist() == ["Vol01/第1話/001.png", "Vol01/第2話/001.png"]
