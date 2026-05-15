"""File hashing utilities for duplicate-safe imports."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path


logger = logging.getLogger(__name__)


class HashService:
    def compute(self, path: Path, algorithm: str = "sha256") -> str:
        logger.debug("Hashing %s (%s)", path, algorithm)
        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            logger.error("Unsupported hash algorithm: %s", algorithm)
            raise ValueError(f"Unsupported hash algorithm: {algorithm}") from exc

        start = time.perf_counter()
        total = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                total += len(chunk)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.debug("Hashed %s: %d bytes in %.0f ms", path, total, elapsed_ms)
        return digest.hexdigest()
