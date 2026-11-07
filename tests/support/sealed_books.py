"""Small sealed Hidden Space books for tests.

Pages are tiny PNGs and the chunk size is the 1 KiB minimum, so a few pages
already span many chunks: that is what exercises reads that start or end
inside a chunk, and damage that should fail only the page it touches.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

from PIL import Image

from joyread.core.vault import ContentType, SealedWriter, VaultPrivateKey

#: The smallest chunk a reader accepts. Real files use 64 KiB.
TEST_CHUNK_LOG2 = 10
_EPOCH = (1980, 1, 1, 0, 0, 0)


def png_page(index: int, *, size: tuple[int, int] = (48, 64)) -> bytes:
    """A distinct, noisy PNG, large enough to span several 1 KiB chunks."""

    image = Image.effect_noise(size, 40 + index).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def canonical_cbz(
    pages: list[bytes],
    *,
    sidecar: bytes | None = None,
    nested: bytes | None = None,
) -> bytes:
    """A CBZ laid out like the canonical writer's: stored pages, deflated sidecar."""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, page in enumerate(pages):
            info = zipfile.ZipInfo(f"{index:03d}.png", date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, page)
        if sidecar is not None:
            info = zipfile.ZipInfo("ComicInfo.xml", date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, sidecar)
        if nested is not None:
            # Something JoyRead never writes into a sealed book.
            archive.writestr(zipfile.ZipInfo("extra/nested.zip", date_time=_EPOCH), nested)
    return buffer.getvalue()


def seal_file(
    path: Path,
    content: bytes,
    key: VaultPrivateKey,
    content_type: ContentType,
    *,
    chunk_log2: int = TEST_CHUNK_LOG2,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as sink:
        writer = SealedWriter(sink, key.public_key, content_type, chunk_log2=chunk_log2)
        writer.write(content)
        writer.finish()
    return path
