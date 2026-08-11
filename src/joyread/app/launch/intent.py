"""Qt-free launch requests shared by bootstrap and local IPC."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
from typing import Iterable

from joyread.core.file_types import SUPPORTED_READER_EXTENSIONS


LAUNCH_PROTOCOL_VERSION = 1
MAX_LAUNCH_MESSAGE_BYTES = 256 * 1024


class LaunchAction(StrEnum):
    SHOW_LIBRARY = "show_library"
    OPEN_FILES = "open_files"


@dataclass(frozen=True)
class LaunchIntent:
    action: LaunchAction
    paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.action == LaunchAction.SHOW_LIBRARY and self.paths:
            raise ValueError("SHOW_LIBRARY cannot carry file paths.")
        if self.action == LaunchAction.OPEN_FILES and not self.paths:
            raise ValueError("OPEN_FILES requires at least one supported path.")

    @classmethod
    def show_library(cls) -> LaunchIntent:
        return cls(LaunchAction.SHOW_LIBRARY)

    @classmethod
    def open_files(
        cls,
        paths: Iterable[str | Path],
        *,
        supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
    ) -> LaunchIntent:
        normalized = normalize_launch_paths(paths, supported_extensions=supported_extensions)
        if not normalized:
            raise ValueError("OPEN_FILES requires at least one supported path.")
        return cls(LaunchAction.OPEN_FILES, normalized)


def intent_from_arguments(
    arguments: Iterable[str],
    *,
    supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
) -> LaunchIntent | None:
    paths = normalize_launch_paths(arguments, supported_extensions=supported_extensions)
    return LaunchIntent(LaunchAction.OPEN_FILES, paths) if paths else None


def normalize_launch_paths(
    paths: Iterable[str | Path],
    *,
    supported_extensions: Iterable[str] = SUPPORTED_READER_EXTENSIONS,
) -> tuple[Path, ...]:
    extensions = frozenset(str(extension).lower() for extension in supported_extensions)
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(value).expanduser()
        if path.suffix.lower() not in extensions:
            continue
        resolved = path.resolve(strict=False)
        key = canonical_path_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(resolved)
    return tuple(normalized)


def canonical_path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def merge_open_intents(*intents: LaunchIntent | None) -> LaunchIntent | None:
    paths: list[Path] = []
    for intent in intents:
        if intent is not None and intent.action == LaunchAction.OPEN_FILES:
            paths.extend(intent.paths)
    normalized = normalize_launch_paths(paths)
    return LaunchIntent(LaunchAction.OPEN_FILES, normalized) if normalized else None


def encode_launch_intent(intent: LaunchIntent) -> bytes:
    payload = {
        "version": LAUNCH_PROTOCOL_VERSION,
        "action": intent.action.value,
        "paths": [str(path) for path in intent.paths],
    }
    # POSIX argv can contain surrogate-escaped filenames. ASCII JSON escaping
    # keeps the local transport valid UTF-8 while preserving those code points.
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > MAX_LAUNCH_MESSAGE_BYTES:
        raise ValueError("Launch request exceeds the maximum IPC message size.")
    return encoded


def decode_launch_intent(data: bytes) -> LaunchIntent:
    if not data or len(data) > MAX_LAUNCH_MESSAGE_BYTES:
        raise ValueError("Invalid launch request size.")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Launch request is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != LAUNCH_PROTOCOL_VERSION:
        raise ValueError("Unsupported launch request protocol.")
    try:
        action = LaunchAction(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Unknown launch request action.") from exc

    raw_paths = payload.get("paths", [])
    if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
        raise ValueError("Launch request paths must be a list of strings.")
    if action == LaunchAction.SHOW_LIBRARY:
        if raw_paths:
            raise ValueError("SHOW_LIBRARY cannot carry file paths.")
        return LaunchIntent.show_library()
    return LaunchIntent.open_files(raw_paths)
