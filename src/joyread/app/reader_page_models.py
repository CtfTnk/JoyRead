"""Qt-free page requests, immutable results, and preparation ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from joyread.app.tasking import TaskPriority


FrameT = TypeVar("FrameT")


@dataclass(frozen=True)
class ReaderPageRequest:
    page_index: int
    target_width: int
    target_height: int
    device_pixel_ratio: float
    generation: int
    priority: TaskPriority = TaskPriority.HIGH


@dataclass(frozen=True)
class ReaderPagePayload:
    page_index: int
    image_bytes: bytes
    source_dimensions: tuple[int, int]


@dataclass(frozen=True)
class PreparedReaderPage(Generic[FrameT]):
    page_index: int
    frame: FrameT
    source_dimensions: tuple[int, int]
    rendered_dimensions: tuple[int, int]
    generation: int

    @property
    def dimensions(self) -> tuple[int, int]:
        """Compatibility name consumed by the pure layout engine."""

        return self.source_dimensions


class ReaderDocumentSource(Protocol):
    page_count: int

    def read_page(self, page_index: int) -> ReaderPagePayload | None: ...

    def close(self) -> None: ...


class PageFrameDecoder(Protocol[FrameT]):
    def decode(
        self,
        payload: ReaderPagePayload,
        request: ReaderPageRequest,
    ) -> PreparedReaderPage[FrameT]: ...


class ReaderFrameCache(Protocol[FrameT]):
    def get_suitable(
        self,
        page_index: int,
        target_width: int,
        target_height: int,
    ) -> PreparedReaderPage[FrameT] | None: ...

    def put(
        self,
        page_index: int,
        value: PreparedReaderPage[FrameT],
        target_width: int,
        target_height: int,
    ) -> None: ...

    def clear(self) -> int: ...
