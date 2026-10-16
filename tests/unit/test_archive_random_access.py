from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import py7zr
from PIL import Image

from joyread.app.reader_document_runtime import ReaderDocumentRuntime
from joyread.core.archive import ArchiveImageService, ArchiveOpenLimits
from joyread.core.archive.formats.rar_backend import rar_requires_sequential_warmup
from joyread.core.archive.formats.seven_zip_backend import _listing_from_archive
from joyread.core.archive.records import ArchiveSource, PageRecord
from joyread.core.archive.session import ArchiveImageSession
from joyread.core.reader.session_service import ReaderSessionService


@pytest.mark.parametrize('suffix', ['.zip', '.rar', '.7z'])
@pytest.mark.parametrize('confirmed,sequential,expected', [
    (False, False, False), (False, True, False), (True, False, True), (True, True, False),
])
def test_positive_source_evidence_is_required_independently_of_warmup(suffix, confirmed, sequential, expected):
    source = ArchiveSource('book', suffix, data=b'', random_access_confirmed=confirmed,
                           requires_sequential_warmup=sequential)
    session = ArchiveImageSession((PageRecord('page', source, 'page.png', None),), lambda *_: {})
    try:
        # No extraction lease: dynamic warmup is false even for solid sources.
        assert not session.requires_sequential_warmup
        assert session.allows_parallel_page_preparation(0) is expected
        assert not session.allows_parallel_page_preparation(-1)
    finally:
        session.close()


@pytest.mark.parametrize('solid', [None, 0, '', 'false', True, False])
def test_seven_zip_unknown_metadata_never_implies_non_solid(solid):
    archive = SimpleNamespace(archiveinfo=lambda: SimpleNamespace(solid=solid), list=lambda: [])
    listing = _listing_from_archive(archive, password=None)
    assert listing.random_access_confirmed is (solid is False)


def test_rar_one_known_member_does_not_override_an_unknown_member():
    def member(flags):
        return SimpleNamespace(isdir=lambda: False, file_compress_flags=flags, flags=None)
    archive = SimpleNamespace(infolist=lambda: [member(0), member(None)])
    assert rar_requires_sequential_warmup(SimpleNamespace(), archive)


def test_real_zip_with_solid_nested_7z_routes_by_actual_source(tmp_path):
    data = BytesIO()
    Image.new('RGB', (16, 24), 'red').save(data, format='PNG')
    nested = tmp_path / 'solid.7z'
    with py7zr.SevenZipFile(nested, 'w') as archive:
        archive.writestr(data.getvalue(), '1.png')
        archive.writestr(data.getvalue(), '2.png')
    outer = tmp_path / 'outer.cbz'
    with ZipFile(outer, 'w') as archive:
        archive.writestr('0.png', data.getvalue())
        archive.writestr('nested.7z', nested.read_bytes())
    runtime = ReaderDocumentRuntime(ReaderSessionService(ArchiveImageService()))
    document = runtime.open_document(outer, passwords={}, skipped_archives=set(), limits=ArchiveOpenLimits())
    try:
        assert document.page_count == 3
        assert [document.allows_parallel_page_preparation(i) for i in range(3)] == [True, False, False]
    finally:
        document.close()
