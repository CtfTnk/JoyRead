"""Per-profile single-instance arbitration and local launch-intent transport."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from hashlib import sha256
import logging
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QLockFile, QObject, QThread
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from joyread.app.launch.intent import (
    MAX_LAUNCH_MESSAGE_BYTES,
    LaunchIntent,
    decode_launch_intent,
    encode_launch_intent,
)


logger = logging.getLogger(__name__)

_MESSAGE_DELIMITER = b"\n"


class InstanceRole(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class SingleInstanceError(RuntimeError):
    """Raised when JoyRead cannot safely become or contact the primary process."""


IntentHandler = Callable[[LaunchIntent], None]

#: Produces the intent to forward, called only if this process is secondary.
#: Deferring the decision matters on macOS, where the document this process was
#: launched to open has not been delivered yet when arbitration happens.
SecondaryIntentFactory = Callable[[], LaunchIntent]


class SingleInstanceBroker(QObject):
    """Use a process lock as authority and QLocalSocket for launch requests."""

    def __init__(
        self,
        support_root: Path,
        *,
        application_id: str = "io.github.ctftnk.joyread",
        connect_timeout_ms: int = 3000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._support_root = support_root.expanduser().resolve()
        profile_hash = sha256(str(self._support_root).encode("utf-8")).hexdigest()[:16]
        self._server_name = f"{application_id}.{profile_hash}"
        self._lock_path = self._support_root / "Config" / ".joyread-instance.lock"
        self._connect_timeout_ms = max(250, connect_timeout_ms)
        self._lock: QLockFile | None = None
        self._server: QLocalServer | None = None
        self._buffers: dict[int, tuple[QLocalSocket, bytearray]] = {}
        self._pending_intents: list[LaunchIntent] = []
        self._intent_handler: IntentHandler | None = None
        self._role: InstanceRole | None = None
        self._disposed = False

    @property
    def role(self) -> InstanceRole | None:
        return self._role

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def start(self, secondary_intent: SecondaryIntentFactory) -> InstanceRole:
        """Claim the profile, or forward this launch to whoever already holds it.

        ``secondary_intent`` is a factory rather than a value so that a process
        which turns out to be primary never pays for resolving it, and one that
        turns out to be secondary can take as long as the platform needs before
        deciding what to say.
        """

        if self._role is not None:
            raise RuntimeError("SingleInstanceBroker.start() may only be called once.")
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SingleInstanceError(f"JoyRead cannot create its instance directory: {exc}") from exc

        lock = QLockFile(str(self._lock_path))
        lock.setStaleLockTime(0)
        self._lock = lock
        if lock.tryLock(0):
            self._start_primary_server()
            self._role = InstanceRole.PRIMARY
            logger.info("Single-instance primary ready server=%s", self._server_name)
            return self._role

        if lock.error() != QLockFile.LockError.LockFailedError:
            raise SingleInstanceError(
                f"JoyRead cannot acquire its instance lock ({lock.error().name})."
            )

        self._forward_to_primary(secondary_intent())
        self._role = InstanceRole.SECONDARY
        logger.info("Launch request forwarded to primary server=%s", self._server_name)
        return self._role

    def set_intent_handler(self, handler: IntentHandler) -> None:
        self._intent_handler = handler
        pending, self._pending_intents = self._pending_intents, []
        for intent in pending:
            self._deliver_intent(intent)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for socket, _buffer in tuple(self._buffers.values()):
            socket.abort()
            socket.deleteLater()
        self._buffers.clear()
        self._pending_intents.clear()
        self._intent_handler = None
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if self._role == InstanceRole.PRIMARY:
            QLocalServer.removeServer(self._server_name)
        if self._lock is not None and self._lock.isLocked():
            self._lock.unlock()
        self._lock = None

    def _start_primary_server(self) -> None:
        # Holding the profile lock makes stale socket removal race-free: no
        # other compliant JoyRead process can be listening for this profile.
        QLocalServer.removeServer(self._server_name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._server_name):
            error = server.errorString()
            if self._lock is not None:
                self._lock.unlock()
            raise SingleInstanceError(f"JoyRead cannot start its local instance server: {error}")
        server.newConnection.connect(self._accept_connections)
        self._server = server

    def _forward_to_primary(self, intent: LaunchIntent) -> None:
        try:
            message = encode_launch_intent(intent) + _MESSAGE_DELIMITER
        except (UnicodeError, ValueError) as exc:
            raise SingleInstanceError(
                f"JoyRead cannot forward this launch request: {exc}"
            ) from exc
        deadline = monotonic() + self._connect_timeout_ms / 1000
        last_error = "connection timed out"
        while monotonic() < deadline:
            socket = QLocalSocket()
            socket.connectToServer(self._server_name)
            remaining_ms = max(1, int((deadline - monotonic()) * 1000))
            if socket.waitForConnected(min(250, remaining_ms)):
                if socket.write(message) != len(message):
                    last_error = socket.errorString()
                    socket.abort()
                    break
                if not socket.waitForBytesWritten(min(1000, remaining_ms)):
                    last_error = socket.errorString()
                    socket.abort()
                    break
                socket.disconnectFromServer()
                return
            last_error = socket.errorString()
            socket.abort()
            QThread.msleep(50)
        raise SingleInstanceError(
            "JoyRead is already running, but the existing process could not receive "
            f"this launch request ({last_error})."
        )

    def _accept_connections(self) -> None:
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                continue
            key = id(socket)
            self._buffers[key] = (socket, bytearray())
            socket.readyRead.connect(lambda socket=socket: self._read_socket(socket))
            socket.disconnected.connect(lambda socket=socket: self._finish_socket(socket))
            self._read_socket(socket)

    def _read_socket(self, socket: QLocalSocket) -> None:
        entry = self._buffers.get(id(socket))
        if entry is None:
            return
        buffer = entry[1]
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > MAX_LAUNCH_MESSAGE_BYTES + len(_MESSAGE_DELIMITER):
            logger.warning("Dropping oversized local launch request")
            socket.abort()
            self._forget_socket(socket)
            return
        while _MESSAGE_DELIMITER in buffer:
            raw, remainder = bytes(buffer).split(_MESSAGE_DELIMITER, 1)
            buffer.clear()
            buffer.extend(remainder)
            self._dispatch_message(raw)

    def _finish_socket(self, socket: QLocalSocket) -> None:
        self._read_socket(socket)
        entry = self._buffers.get(id(socket))
        if entry is not None and entry[1]:
            logger.warning("Dropping incomplete local launch request")
        self._forget_socket(socket)

    def _forget_socket(self, socket: QLocalSocket) -> None:
        self._buffers.pop(id(socket), None)
        socket.deleteLater()

    def _dispatch_message(self, raw: bytes) -> None:
        try:
            intent = decode_launch_intent(raw)
        except ValueError as exc:
            logger.warning("Ignoring invalid local launch request: %s", exc)
            return
        handler = self._intent_handler
        if handler is None:
            self._pending_intents.append(intent)
            return
        self._deliver_intent(intent)

    def _deliver_intent(self, intent: LaunchIntent) -> None:
        handler = self._intent_handler
        if handler is None:
            self._pending_intents.append(intent)
            return
        try:
            handler(intent)
        except Exception:
            # Local launch intents must not be able to unwind through Qt's
            # readyRead signal. Keep the primary alive for later requests.
            logger.exception(
                "Launch intent handler failed action=%s paths=%d",
                intent.action,
                len(intent.paths),
            )
