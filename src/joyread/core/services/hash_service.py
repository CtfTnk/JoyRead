"""File hashing utilities for duplicate-safe imports."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path


logger = logging.getLogger(__name__)


class HashService:
    _CHUNK_SIZE = 1024 * 1024

    def compute(self, path: Path, algorithm: str = "sha256") -> str:
        logger.debug("Hashing %s (%s)", path, algorithm)
        digest = self._new_digest(algorithm)

        start = time.perf_counter()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(self._CHUNK_SIZE), b""):
                digest.update(chunk)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.debug("Hashed %s in %.0f ms", path, elapsed_ms)
        return digest.hexdigest()

    def copy_with_hash(self, source: Path, destination: Path, algorithm: str = "sha256") -> str:
        """Stream-copy bytes while producing the destination content digest.

        This is the import's normal single-pass path. The caller owns cleanup
        of ``destination`` when a duplicate, probe failure, or source mutation
        is discovered after copying.
        """

        digest = self._new_digest(algorithm)
        start = time.perf_counter()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            for chunk in iter(lambda: input_stream.read(self._CHUNK_SIZE), b""):
                digest.update(chunk)
                output_stream.write(chunk)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.debug("Copied and hashed %s in %.0f ms", source, elapsed_ms)
        return digest.hexdigest()

    @staticmethod
    def _new_digest(algorithm: str):
        try:
            return hashlib.new(algorithm)
        except ValueError as exc:
            logger.error("Unsupported hash algorithm: %s", algorithm)
            raise ValueError(f"Unsupported hash algorithm: {algorithm}") from exc
