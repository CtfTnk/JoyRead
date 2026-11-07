"""A ``QIODevice`` that lets QtPdf read a sealed PDF without decrypting it to disk.

``QPdfDocument.load(device)`` on a random-access device hands PDFium a block
callback instead of a file, and each block decrypts only the 64 KiB chunks it
covers. Nothing is ever written out.

It is not lazy, though. While loading, QtPdf asks PDFium whether every page
is available, and that reads the whole document -- measured, all 24 MB of a
24 MB PDF in 153 reads, after which a render reads nothing at all. Loading a
plain PDF by path behaves the same way, so a sealed PDF costs the same memory
as an ordinary one plus one decryption pass. The consequence that matters is
that PDFium then holds the plaintext: revoking the file stops nothing on its
own, which is why a sealed ``PdfImageSession`` checks for revocation itself.

Where reads come from matters. QtPdf serializes every PDFium call behind one
global mutex, and a render runs on ``QPdfPageRenderer``'s own worker thread,
not on the PDF thread that loaded the document. So ``readData`` is entered
from more than one thread over a document's life, but never from two at once.
The lock below makes that independent of QtPdf's internals rather than
relying on them. Each call briefly takes the GIL; decryption itself runs in
C with the GIL released.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QIODevice, QObject

from joyread.core.vault import SealedFile, VaultError


logger = logging.getLogger(__name__)


class SealedPdfDevice(QIODevice):
    """Read-only, seekable view of a sealed PDF's content.

    Open it with ``ReadOnly | Unbuffered``: PDFium seeks constantly, and Qt's
    read-ahead buffer would only decrypt bytes it then throws away.

    A read that fails returns no data instead of raising. An exception cannot
    unwind back through PDFium, and an empty block makes PDFium fail that load
    or that render, which surfaces as an ordinary PDF error. Failures are the
    vault locking under an open book (the keyring revokes the file) or a
    damaged chunk; both are counted so a caller can tell which kind of empty
    page it got.
    """

    def __init__(self, sealed: SealedFile, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._view = sealed.open_content()
        self._size = sealed.content_length
        self._lock = threading.Lock()
        self.failed_reads = 0
        self.last_error: VaultError | None = None

    def isSequential(self) -> bool:  # noqa: N802 - Qt override.
        return False

    def size(self) -> int:
        return self._size

    def readData(self, maxlen: int) -> bytes:  # noqa: N802 - Qt override.
        position = self.pos()
        length = min(int(maxlen), self._size - position)
        if length <= 0:
            return b""
        try:
            with self._lock:
                self._view.seek(position)
                return self._view.read(length) or b""
        except VaultError as exc:
            self._record_failure(exc)
        except (OSError, ValueError) as exc:
            self._record_failure(None, exc)
        return b""

    def writeData(self, data: bytes) -> int:  # noqa: N802 - Qt override.
        del data
        return -1

    def _record_failure(self, error: VaultError | None, other: BaseException | None = None) -> None:
        self.failed_reads += 1
        if error is not None:
            self.last_error = error
        if self.failed_reads == 1:
            # Once per device: PDFium retries freely, and a revoked book would
            # otherwise log a line for every block it asks for.
            logger.info(
                "A sealed PDF read failed",
                extra={
                    "event": "pdf.sealed.read_failed",
                    "category": "pdf",
                    "status": "failed",
                    "error_type": type(error if error is not None else other).__name__,
                },
            )


__all__ = ["SealedPdfDevice"]
