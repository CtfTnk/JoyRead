"""Regression coverage for the dedicated Windows file-association icon."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_windows_document_icon.py"
SOURCE_SVG = REPO_ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyReadDocument.svg"
DOCUMENT_ICON = REPO_ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyReadDocument.ico"
APP_ICON = REPO_ROOT / "src" / "joyread" / "ui" / "resources" / "icons" / "JoyRead.ico"
EXPECTED_SIZES = {
    (16, 16),
    (20, 20),
    (24, 24),
    (32, 32),
    (40, 40),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
}


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_windows_document_icon", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_document_icon_has_a_self_contained_vector_source() -> None:
    root = ET.parse(SOURCE_SVG).getroot()
    elements = list(root.iter())
    fills = {element.attrib.get("fill") for element in elements}
    mark = next(
        element for element in elements if element.attrib.get("id") == "joyread-document-mark"
    )

    assert root.attrib["viewBox"] == "0 0 256 256"
    assert {"#F6F3F2", "#DCEBFF", "#293242"} <= fills
    assert mark.attrib["width"] == "100"
    assert mark.attrib["height"] == "100"
    assert mark.attrib["rx"] == "18"
    assert mark.attrib["fill"] == "#293242"
    assert not any("href" in attribute.casefold() for element in elements for attribute in element.attrib)
    assert not any(element.tag.endswith("script") for element in elements)


def test_document_icon_is_a_distinct_native_multisize_windows_resource() -> None:
    with Image.open(DOCUMENT_ICON) as icon:
        assert icon.format == "ICO"
        assert icon.mode == "RGBA"
        assert icon.info["sizes"] == EXPECTED_SIZES

    assert DOCUMENT_ICON.read_bytes() != APP_ICON.read_bytes()


def test_document_icon_generator_renders_every_shell_size(tmp_path: Path) -> None:
    builder = _load_builder()
    destination = tmp_path / "JoyReadDocument.ico"

    assert builder.build_icon(destination=destination) == destination
    with Image.open(destination) as icon:
        assert icon.info["sizes"] == EXPECTED_SIZES
