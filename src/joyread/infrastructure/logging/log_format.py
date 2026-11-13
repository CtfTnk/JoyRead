"""The text layout of JoyRead's log files: written for people, parseable.

A launch's log opens with header lines and then has one line per record::

    # JoyRead 1.4.0 · run c302c244c4c24ea0873e746afa5d5f4d · pid 14121 · started 2026-11-13 21:11:49 +0800
    # Local time. Each line: time, level, category, message · event and fields (where in the code).
    21:11:49.657 INFO    process  JoyRead process starting · process.starting status=started outcome="platform=Darwin python=3.12.9"  (app.bootstrap:241)
    21:11:50.430 WARNING reader   Page decode failed · reader.page.failed page=12 path="/Users/alice/Books/A B.cbz"  (core.reader.pages:88, PageWorker-2)
        Traceback (most recent call last):
          ...

Everything a launch shares -- run id, pid, version, start time -- is said
once in the header, not on every line. Times are local; a ``# YYYY-MM-DD``
line marks a change of day. A field value is written bare when that is
unambiguous and as a JSON string or array otherwise, so it stays on one line
and reads back exactly. Indented lines continue the record above: the rest of
a multi-line message, then any traceback.

:func:`read_log_records` reads a file back into dictionaries, for tests and
for tools that summarise a log. This module is Qt-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path


HEADER_PREFIX = "# "
CONTINUATION_INDENT = "    "
EVENT_SEPARATOR = " · "
LOCATION_SEPARATOR = "  ("
#: The event a plain ``logger.info(...)`` call gets; not worth repeating.
PLAIN_EVENT = "log.message"
_LEVEL_WIDTH = 7
_CATEGORY_WIDTH = 8
_BARE_VALUE_RE = re.compile(r'^[^\s="\[\]·()]+$')
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

_RECORD_RE = re.compile(
    r"^(?P<time>\d{2}:\d{2}:\d{2}\.\d{3}) (?P<level>[A-Z]+) +(?P<category>\S+) +(?P<rest>.*?)"
    r"  \((?P<logger>[^():,]+):(?P<lineno>\d+)(?:, (?P<thread>[^()]*))?\)$"
)
_DATE_RE = re.compile(r"^# (?P<date>\d{4}-\d{2}-\d{2})$")
_TOKEN_RE = re.compile(
    r'(?:(?P<key>[A-Za-z_][A-Za-z0-9_.]*)=)?'
    r'(?P<value>"(?:[^"\\]|\\.)*"|\[(?:[^\[\]"]|"(?:[^"\\]|\\.)*")*\]|[^\s"\[·][^\s·]*)'
)


def header_lines(
    *,
    app_version: str,
    build_id: str | None,
    run_id: str,
    pid: int,
    started_at: datetime,
) -> list[str]:
    build = f" (build {build_id})" if build_id else ""
    started = started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    return [
        f"{HEADER_PREFIX}JoyRead {app_version}{build} · run {run_id} · pid {pid} · started {started}",
        f"{HEADER_PREFIX}Local time. Each line: time, level, category, message"
        f"{EVENT_SEPARATOR}event and fields (where in the code).",
    ]


def date_line(day: date) -> str:
    return f"{HEADER_PREFIX}{day.isoformat()}"


def format_value(value: object) -> str:
    """One field value, on one line, readable back by :func:`parse_value`."""

    if isinstance(value, str):
        return value if _BARE_VALUE_RE.match(value) else json.dumps(value, ensure_ascii=False)
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"), default=str)
    return format_value(str(value))


def parse_value(text: str) -> object:
    if text.startswith(('"', "[")) or text in {"true", "false", "null"}:
        try:
            return json.loads(text)
        except ValueError:
            return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def format_record(
    *,
    moment: datetime,
    level: str,
    category: str,
    message: str,
    event: str | None,
    fields: Sequence[tuple[str, object]],
    location: str,
    thread: str | None,
    detail: str | None,
) -> str:
    """One record: the line, then indented continuation lines if any."""

    first, *more = message.splitlines() or [""]
    words = [] if event in (None, PLAIN_EVENT) else [event]
    if not words and EVENT_SEPARATOR in first:
        # Without fields the parser could not tell the message's own
        # separator from the one before the fields; name the event.
        words = [PLAIN_EVENT]
    words.extend(
        f"{key}={format_value(value)}" for key, value in fields if _KEY_RE.match(key)
    )
    tail = f"{EVENT_SEPARATOR}{' '.join(words)}" if words else ""
    where = f"{location}, {thread}" if thread else location
    line = (
        f"{moment:%H:%M:%S}.{moment.microsecond // 1000:03d} "
        f"{level:<{_LEVEL_WIDTH}} {category:<{_CATEGORY_WIDTH}} {first}{tail}  ({where})"
    )
    continuation = list(more)
    if detail:
        continuation.extend(detail.rstrip("\n").splitlines())
    return "\n".join([line, *(CONTINUATION_INDENT + item for item in continuation)])


def parse_log_lines(lines: Iterable[str]) -> list[dict[str, object]]:
    """Records from the lines of a log file, oldest first.

    Each record has ``date`` (when known), ``time``, ``level``, ``category``,
    ``message``, ``event``, ``logger``, ``lineno``, ``thread``, its fields by
    name, and ``detail`` (continuation lines, e.g. a traceback) when present.
    A line that fits no shape is kept as a record with only ``raw``.
    """

    records: list[dict[str, object]] = []
    current_date: str | None = None
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith(CONTINUATION_INDENT) and records:
            previous = records[-1]
            text = line[len(CONTINUATION_INDENT):]
            previous["detail"] = f"{previous['detail']}\n{text}" if "detail" in previous else text
            continue
        if line.startswith("#"):
            if (match := _DATE_RE.match(line)) is not None:
                current_date = match["date"]
            elif current_date is None and (started := _header_date(line)) is not None:
                current_date = started
            continue
        match = _RECORD_RE.match(line)
        if match is None:
            records.append({"raw": line})
            continue
        records.append(_record_from_match(match, current_date))
    return records


def read_log_records(path: Path) -> list[dict[str, object]]:
    return parse_log_lines(path.read_text(encoding="utf-8").splitlines())


def _record_from_match(match: re.Match[str], current_date: str | None) -> dict[str, object]:
    message, tail = _split_tail(match["rest"])
    record: dict[str, object] = {
        "date": current_date,
        "time": match["time"],
        "level": match["level"],
        "category": match["category"],
        "message": message,
        "event": PLAIN_EVENT,
        "logger": match["logger"],
        "lineno": int(match["lineno"]),
        "thread": match["thread"] or "MainThread",
    }
    for index, token in enumerate(_TOKEN_RE.finditer(tail)):
        key, value = token["key"], token["value"]
        if key is None:
            if index == 0:
                record["event"] = value
            continue
        record[key] = parse_value(value)
    return record


def _split_tail(rest: str) -> tuple[str, str]:
    """Message and fields: the first separator after which only fields follow.

    A message may itself contain the separator, and a quoted value may too, so
    the split is the leftmost one whose remainder is entirely tokens.
    """

    bare = EVENT_SEPARATOR.lstrip()
    if rest.startswith(bare) and _is_token_run(rest[len(bare):]):
        return "", rest[len(bare):]  # An empty message: the layout's padding ate the space.
    start = 0
    while (index := rest.find(EVENT_SEPARATOR, start)) >= 0:
        tail = rest[index + len(EVENT_SEPARATOR):]
        if _is_token_run(tail):
            return rest[:index], tail
        start = index + 1
    return rest, ""


def _is_token_run(text: str) -> bool:
    position = 0
    for token in _TOKEN_RE.finditer(text):
        if token.start() != position:
            return False
        position = token.end() + 1
    return position == len(text) + 1 and bool(text)


def _header_date(line: str) -> str | None:
    match = re.search(r"· started (\d{4}-\d{2}-\d{2}) ", line)
    return match.group(1) if match else None


__all__ = [
    "CONTINUATION_INDENT",
    "EVENT_SEPARATOR",
    "HEADER_PREFIX",
    "PLAIN_EVENT",
    "date_line",
    "format_record",
    "format_value",
    "header_lines",
    "parse_log_lines",
    "parse_value",
    "read_log_records",
]
