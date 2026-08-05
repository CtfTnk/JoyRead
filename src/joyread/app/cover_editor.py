"""Qt-free contracts for preparing cover-editor source previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


FrameT = TypeVar("FrameT")
SizeTuple = tuple[int, int]


@dataclass(frozen=True)
class PreparedCoverSource(Generic[FrameT]):
    """A bounded worker-prepared preview plus immutable source metadata."""

    source_token: str
    frame: FrameT
    source_dimensions: SizeTuple


class CoverPreviewRenderer(Protocol[FrameT]):
    """Infrastructure port for decoding a bounded cover-editor preview."""

    def prepare_preview(
        self,
        image_bytes: bytes,
        size: SizeTuple,
        source_token: str,
    ) -> PreparedCoverSource[FrameT]: ...
