"""Guards for the reproducible Windows MSI definition."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_windows_msi.py"
WIX_SOURCE = REPO_ROOT / "packaging" / "windows" / "JoyRead.wxs"
WIX_NS = {"w": "http://wixtoolset.org/schemas/v4/wxs"}


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_windows_msi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_msi_build_command_is_pinned_to_x64_and_the_onedir_tree() -> None:
    builder = _load_builder()
    command = builder.wix_build_command("1.2.3", Path("result.msi"))

    assert command[:5] == ["dotnet", "tool", "run", "wix", "--"]
    assert command[command.index("-arch") + 1] == "x64"
    assert "Version=1.2.3" in command
    assert f"AppDir={builder.APP_DIR}" in command
    assert command[-1] == str(builder.WIX_SOURCE)


def test_msi_registers_only_reader_specific_extensions_as_candidates() -> None:
    root = ET.parse(WIX_SOURCE).getroot()
    names = {
        value.attrib["Name"]
        for value in root.findall(".//w:RegistryValue", WIX_NS)
        if value.attrib.get("Value") == "JoyRead.Document"
    }

    assert names == {".cbz", ".cbr", ".cb7", ".pdf"}
    assert not names & {".zip", ".rar", ".7z"}


def test_msi_does_not_write_an_extension_default_value() -> None:
    root = ET.parse(WIX_SOURCE).getroot()
    extension_keys = [
        key.attrib["Key"]
        for key in root.findall(".//w:RegistryKey", WIX_NS)
        if "Software\\Classes\\." in key.attrib.get("Key", "")
    ]

    assert extension_keys
    assert all(key.endswith("\\OpenWithProgids") for key in extension_keys)


def test_start_menu_shortcut_is_advertised_from_the_exe_keypath() -> None:
    root = ET.parse(WIX_SOURCE).getroot()
    component = root.find(".//w:Component[@Id='ApplicationExecutable']", WIX_NS)

    assert component is not None
    executable = component.find("w:File[@Id='JoyReadExe']", WIX_NS)
    assert executable is not None and executable.attrib["KeyPath"] == "yes"
    shortcut = executable.find("w:Shortcut", WIX_NS)
    assert shortcut is not None and shortcut.attrib["Advertise"] == "yes"


def test_file_association_component_does_not_cross_reference_the_exe_component() -> None:
    root = ET.parse(WIX_SOURCE).getroot()
    component = root.find(".//w:Component[@Id='FileAssociations']", WIX_NS)

    assert component is not None
    values = [value.attrib.get("Value", "") for value in component.findall(".//w:RegistryValue", WIX_NS)]
    assert all("[#JoyReadExe]" not in value for value in values)
    assert any("[INSTALLFOLDER]JoyRead.exe" in value for value in values)


def test_pyinstaller_spec_bundles_project_license() -> None:
    spec_text = (REPO_ROOT / "packaging" / "joyread.spec").read_text(encoding="utf-8")

    assert '(str(ROOT / "LICENSE"), ".")' in spec_text
