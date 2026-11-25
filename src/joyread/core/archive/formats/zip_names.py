"""ZIP member names as their authors wrote them.

``zipfile`` (and pyzipper, which copies it) reads a name without the UTF-8
flag as cp437, which garbles every Shift-JIS, GBK or Big5 name. This module
recovers the stored bytes and reads them properly. In order:

1. A name with the UTF-8 flag, or one that is plain ASCII, is already right.
2. An Info-ZIP Unicode Path extra field (0x7075) carries a UTF-8 copy of the
   name. It is used when its CRC proves it describes the stored name.
3. Every other name is read with one codec chosen for the whole archive by
   :func:`joyread.core.archive.name_encoding.choose_legacy_codec`.

**Names an extractor cannot address.** 7-Zip selects members by name, and it
does not read legacy names the way this module does. The bundled 7zz on macOS
and Linux ignores ``-mcp`` and escapes every non-UTF-8 byte into a private-use
character. 7z.exe on Windows reads with the system's OEM code page. Either way,
a decoded name handed to it matches nothing. Before this module existed the
cp437 name failed the same way, so ZipCrypto pages with Japanese names did not
open at all. A member is therefore marked ``extractor_addressable`` only when
its name is UTF-8 or ASCII as stored. The backend reads the rest in process,
and the listing withdraws bulk extraction for the archive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
import zlib

from joyread.core.archive.name_encoding import choose_legacy_codec

_UTF8_NAME_FLAG = 0x800
_UNICODE_PATH_EXTRA_ID = 0x7075


@dataclass(frozen=True)
class ZipMember:
    """One central-directory entry and the name JoyRead knows it by."""

    name: str
    info: object
    #: Whether 7-Zip finds this member under ``name``.
    extractor_addressable: bool

    @property
    def file_size(self) -> int | None:
        return getattr(self.info, "file_size", None)


class ZipMemberIndex:
    """Decoded names for one opened archive, and the way back to its entries.

    Built from ``infolist()`` on every open. The listing and every later read
    open the archive separately, and both must derive the same names from the
    same bytes, so nothing is cached between them.
    """

    def __init__(self, infos: Sequence[object]) -> None:
        # Every page read builds one of these over the whole central
        # directory, so the common case -- flagged or ASCII names -- stays a
        # single cheap pass, and member objects are made only on request.
        self._infos = tuple(infos)
        names: list[str] = []
        self._addressable: list[bool] = []
        undeclared: list[tuple[int, bytes, str | None]] = []
        for position, info in enumerate(self._infos):
            if _declares_utf8(info) or getattr(info, "orig_filename", info.filename).isascii():
                names.append(info.filename)
                self._addressable.append(True)
                continue
            raw = _stored_name(info)
            names.append(info.filename)
            self._addressable.append(False)
            undeclared.append((position, raw, _unicode_path(info, raw)))
        if undeclared:
            codec = choose_legacy_codec(raw for _position, raw, path in undeclared if path is None)
            for position, raw, unicode_path in undeclared:
                names[position] = _undeclared_name(self._infos[position], raw, unicode_path, codec)
        self._names = names
        # Last one wins for a repeated name, matching ``ZipFile.getinfo``.
        self._positions = {name: position for position, name in enumerate(names)}

    def files(self) -> tuple[ZipMember, ...]:
        return tuple(
            self._member(position)
            for position, info in enumerate(self._infos)
            if not info.is_dir()
        )

    def member(self, name: str) -> ZipMember:
        """The entry listed under ``name``; KeyError like ``getinfo``."""

        return self._member(self._positions[name])

    @property
    def extractor_addressable(self) -> bool:
        """Whether 7-Zip finds every file entry under its listed name."""

        return all(
            addressable
            for info, addressable in zip(self._infos, self._addressable)
            if not info.is_dir()
        )

    def _member(self, position: int) -> ZipMember:
        return ZipMember(
            self._names[position],
            self._infos[position],
            extractor_addressable=self._addressable[position],
        )


def _undeclared_name(info: object, raw: bytes, unicode_path: str | None, codec: str) -> str:
    if unicode_path is not None:
        # 7-Zip honours this field too, but only the bundled macOS build has
        # been checked, so these members still count as not addressable.
        return _sanitized(unicode_path)
    try:
        return _sanitized(raw.decode(codec))
    except UnicodeDecodeError:
        # Only names past the sample the codec was chosen from can get here.
        return info.filename


def _declares_utf8(info: object) -> bool:
    return bool(getattr(info, "flag_bits", 0) & _UTF8_NAME_FLAG)


def _stored_name(info: object) -> bytes:
    """The name's bytes exactly as the central directory holds them.

    ``orig_filename`` is the cp437 (or flagged UTF-8) reading taken before
    ``ZipInfo`` cut it at NUL and rewrote separators. cp437 maps every byte, so
    encoding back recovers the original bytes exactly.
    """

    original = getattr(info, "orig_filename", info.filename)
    if _declares_utf8(info):
        return original.encode("utf-8")
    return original.encode("cp437")


def _unicode_path(info: object, raw: bytes) -> str | None:
    """The UTF-8 name from an Info-ZIP Unicode Path extra field, if valid.

    Layout: header id, data size, version 1, CRC-32 of the stored name, then
    the UTF-8 name. A CRC that does not match means the stored name was
    changed by a tool that did not update the field, so the field is ignored.
    """

    extra = getattr(info, "extra", b"") or b""
    offset = 0
    while offset + 4 <= len(extra):
        header_id = int.from_bytes(extra[offset : offset + 2], "little")
        size = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        data = extra[offset + 4 : offset + 4 + size]
        offset += 4 + size
        if header_id != _UNICODE_PATH_EXTRA_ID or len(data) < 5 or data[0] != 1:
            continue
        if int.from_bytes(data[1:5], "little") != zlib.crc32(raw):
            return None
        try:
            return data[5:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _sanitized(name: str) -> str:
    """Apply ``ZipInfo``'s own clean-up, so a decoded name is shaped alike."""

    null = name.find("\0")
    if null >= 0:
        name = name[:null]
    if os.sep != "/" and os.sep in name:
        name = name.replace(os.sep, "/")
    return name


__all__ = ["ZipMember", "ZipMemberIndex"]
