from __future__ import annotations

import os
from pathlib import Path
import stat
import struct

import pytest


RESOURCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "joyread"
    / "resources"
    / "extractors"
    / "7zip"
)


def _elf64_machine_and_program_types(path: Path) -> tuple[int, tuple[int, ...]]:
    payload = path.read_bytes()
    assert payload[:4] == b"\x7fELF"
    assert payload[4] == 2, "the bundled Linux helper must be ELF64"
    assert payload[5] == 1, "the bundled Linux helper must be little-endian"

    machine = struct.unpack_from("<H", payload, 18)[0]
    program_offset = struct.unpack_from("<Q", payload, 32)[0]
    program_entry_size = struct.unpack_from("<H", payload, 54)[0]
    program_count = struct.unpack_from("<H", payload, 56)[0]
    program_types = tuple(
        struct.unpack_from("<I", payload, program_offset + index * program_entry_size)[0]
        for index in range(program_count)
    )
    return machine, program_types


@pytest.mark.parametrize(
    ("platform_directory", "expected_machine"),
    (("linux-x86_64", 62), ("linux-arm64", 183)),
)
def test_linux_helpers_are_static_elf_for_the_named_architecture(
    platform_directory: str,
    expected_machine: int,
) -> None:
    helper = RESOURCE_ROOT / platform_directory / "7zz"

    machine, program_types = _elf64_machine_and_program_types(helper)

    assert machine == expected_machine
    assert 3 not in program_types, "PT_INTERP would make this a host-glibc-dependent build"
    if os.name != "nt":
        assert helper.stat().st_mode & stat.S_IXUSR
