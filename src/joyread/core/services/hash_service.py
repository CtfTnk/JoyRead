"""File hashing utilities for duplicate-safe imports."""

from __future__ import annotations

import hashlib
from pathlib import Path


class HashService:
    def compute(self, path: Path, algorithm: str = "sha256") -> str:
        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            raise ValueError(f"Unsupported hash algorithm: {algorithm}") from exc

        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
