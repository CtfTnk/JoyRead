from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from joyread.app.launch.intent import (
    LAUNCH_PROTOCOL_VERSION,
    MAX_LAUNCH_MESSAGE_BYTES,
    LaunchAction,
    LaunchIntent,
    decode_launch_intent,
    encode_launch_intent,
    intent_from_arguments,
)


def test_show_library_round_trips() -> None:
    intent = LaunchIntent.show_library()

    assert decode_launch_intent(encode_launch_intent(intent)) == intent
    assert intent.action == LaunchAction.SHOW_LIBRARY
    assert intent.paths == ()


def test_open_files_preserves_order_deduplicates_and_filters_epub(tmp_path: Path) -> None:
    first = tmp_path / "Chapter.cbz"
    second = tmp_path / "Volume.pdf"
    epub = tmp_path / "Disabled.epub"

    intent = intent_from_arguments((str(first), str(epub), str(first), str(second)))

    assert intent == LaunchIntent.open_files((first, second))
    assert intent is not None
    assert intent.paths == (first.resolve(), second.resolve())
    assert decode_launch_intent(encode_launch_intent(intent)) == intent


def test_unsupported_arguments_do_not_create_an_intent(tmp_path: Path) -> None:
    assert intent_from_arguments((str(tmp_path / "book.epub"), "--unknown")) is None


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"not-json",
        json.dumps({"version": 99, "action": "show_library", "paths": []}).encode(),
        json.dumps(
            {"version": LAUNCH_PROTOCOL_VERSION, "action": "unknown", "paths": []}
        ).encode(),
        json.dumps(
            {
                "version": LAUNCH_PROTOCOL_VERSION,
                "action": "show_library",
                "paths": ["unexpected.cbz"],
            }
        ).encode(),
    ),
)
def test_invalid_launch_messages_are_rejected(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_launch_intent(payload)


def test_oversized_launch_message_is_rejected() -> None:
    with pytest.raises(ValueError):
        decode_launch_intent(b"x" * (MAX_LAUNCH_MESSAGE_BYTES + 1))


@pytest.mark.skipif(os.name == "nt", reason="Windows paths cannot contain surrogate escapes.")
def test_surrogateescaped_posix_path_round_trips_through_ascii_json() -> None:
    source = Path("chapter-\udcff.cbz")
    intent = LaunchIntent.open_files((source,))

    encoded = encode_launch_intent(intent)

    assert b"\\udcff" in encoded
    assert decode_launch_intent(encoded) == intent
