"""Optional Qt codecs must not remove archive image support on older wheels."""

from io import BytesIO

from PIL import Image, ImageCms, ImageOps
from PySide6.QtGui import QImageReader
import pytest

from joyread.app.reader_page_models import ReaderPagePayload, ReaderPageRequest
from joyread.core.services.thumbnail_service import ThumbnailRenderError
from joyread.infrastructure import reader_image_decoder, thumbnail_renderer
from joyread.infrastructure.pillow_image_decoder import decode_pillow_image


@pytest.fixture
def webp():
    image = Image.new("RGBA", (80, 120), (20, 100, 200, 85))
    image.paste((255, 30, 90, 200), (0, 0, 80, 30))
    orientation = Image.Exif()
    orientation[274] = 6
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    output = BytesIO()
    image.save(output, "WEBP", lossless=True, exif=orientation, icc_profile=profile)
    return output.getvalue()


@pytest.fixture
def unavailable_qt_codec(monkeypatch):
    class MissingCodecReader(QImageReader):
        def canRead(self):
            return False

    monkeypatch.setattr(reader_image_decoder, "QImageReader", MissingCodecReader)
    monkeypatch.setattr(thumbnail_renderer, "QImageReader", MissingCodecReader)


def test_fallback_retains_oriented_pixels_alpha_profile_and_owned_storage(webp):
    frame, dimensions = decode_pillow_image(webp, (200, 200))
    with Image.open(BytesIO(webp)) as source:
        expected = ImageOps.exif_transpose(source).convert("RGBA")
    assert dimensions == expected.size == (120, 80)
    assert frame.size().toTuple() == expected.size
    assert bytes(frame.constBits()) == expected.tobytes()
    assert frame.hasAlphaChannel() and frame.colorSpace().isValid()
    # The source and temporary Pillow/bytes objects have already gone away.
    assert frame.pixelColor(10, 10).alpha() == 85


def test_reader_missing_codec_returns_only_a_bounded_hidpi_frame(webp, unavailable_qt_codec):
    request = ReaderPageRequest(0, 60, 60, 2.0, 7)
    result = reader_image_decoder.QtPageFrameDecoder().decode(
        ReaderPagePayload(0, webp, (80, 120)), request,
    )
    assert result.source_dimensions == (120, 80)
    assert result.rendered_dimensions == (60, 40)
    assert result.frame.devicePixelRatio() == 2
    assert result.generation == 7
    assert reader_image_decoder.qimage_frame_bytes(result) == 60 * 40 * 4


def test_thumbnail_and_cover_preview_work_without_the_qt_codec(webp, unavailable_qt_codec):
    renderer = thumbnail_renderer.QtThumbnailRenderer()
    encoded = renderer.render_encoded(webp, (30, 40))
    with Image.open(BytesIO(encoded)) as thumbnail:
        assert thumbnail.size == (30, 40)
        assert thumbnail.format == "PNG"
    preview = renderer.prepare_preview(webp, (60, 60), "original")
    assert preview.source_dimensions == (120, 80)
    assert preview.frame.size().toTuple() == (60, 40)
    assert preview.frame.colorSpace().isValid()
    assert preview.source_token == "original"


@pytest.mark.parametrize("surface", ("reader", "thumbnail", "cover"))
def test_missing_codec_corrupt_input_remains_a_controlled_failure(surface, unavailable_qt_codec):
    if surface == "reader":
        with pytest.raises(RuntimeError, match="Could not decode page 1"):
            reader_image_decoder.QtPageFrameDecoder().decode(
                ReaderPagePayload(0, b"invalid", (80, 120)),
                ReaderPageRequest(0, 60, 60, 1.0, 1),
            )
    else:
        renderer = thumbnail_renderer.QtThumbnailRenderer()
        with pytest.raises(ThumbnailRenderError, match="Could not decode"):
            if surface == "thumbnail":
                renderer.render_encoded(b"invalid", (30, 40))
            else:
                renderer.prepare_preview(b"invalid", (60, 60), "bad")
