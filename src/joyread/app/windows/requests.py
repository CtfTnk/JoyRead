"""Typed requests passed from views to application-level window ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from joyread.core.models.book import Book


@dataclass(frozen=True)
class StandaloneReaderRequest:
    path: Path
    book: Book | None = None
    title: str | None = None
    start_page_index: int | None = None


class StandaloneReaderLauncher(Protocol):
    def __call__(self, request: StandaloneReaderRequest) -> object | None: ...
