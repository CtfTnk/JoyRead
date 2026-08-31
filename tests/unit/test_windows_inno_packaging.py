"""Guards for the reproducible Windows Inno Setup production installer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_windows_inno.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_windows_inno", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_valid_app(builder, root: Path) -> Path:
    app_dir = root / "app"
    for relative in builder.REQUIRED_FILES:
        path = app_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    plugin = app_dir / "PySide6" / "plugins" / "platforms" / "qwindows.dll"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.touch()
    return app_dir


def test_validate_app_dir_accepts_a_complete_onedir(tmp_path: Path) -> None:
    builder = _load_builder()
    app_dir = _make_valid_app(builder, tmp_path)

    builder.validate_app_dir(app_dir)


def test_validate_app_dir_rejects_an_executable_without_its_runtime(tmp_path: Path) -> None:
    builder = _load_builder()
    app_dir = tmp_path / "incomplete"
    app_dir.mkdir()
    (app_dir / "JoyRead.exe").touch()

    with pytest.raises(SystemExit, match="Incomplete PyInstaller app directory"):
        builder.validate_app_dir(app_dir)


def test_inno_command_passes_all_machine_specific_values_as_defines() -> None:
    builder = _load_builder()
    compiler = Path("C:/tools/ISCC.exe")
    app_dir = Path("C:/work/JoyRead/dist/JoyRead")
    destination = Path("C:/work/JoyRead/dist/JoyRead-1.2.3-windows-x86_64-setup.exe")

    command = builder.iscc_command(
        compiler,
        version="1.2.3",
        app_dir=app_dir,
        destination=destination,
    )

    assert command[0] == str(compiler)
    assert f"--define=MyProjectRoot={builder.ROOT}" in command
    assert f"--define=MyAppSourceDir={app_dir}" in command
    assert "--define=MyAppVersion=1.2.3" in command
    assert "--define=MyAppFileVersion=1.2.3.0" in command
    assert f"--output-dir={destination.parent}" in command
    assert f"--output-filename={destination.stem}" in command
    assert command[-1] == str(builder.INNO_SOURCE)


def test_inno_output_matches_the_production_windows_name() -> None:
    builder = _load_builder()

    assert builder.output_path("1.0.0") == (
        builder.ROOT / "dist" / "JoyRead-1.0.0-windows-x86_64-setup.exe"
    )


def test_windows_file_version_is_padded_for_installer_metadata() -> None:
    builder = _load_builder()

    assert builder.windows_file_version("1.0.0") == "1.0.0.0"
    assert builder.windows_file_version("2.5") == "2.5.0.0"
    assert builder.windows_file_version("3.1.4rc1") == "3.1.4.0"


def test_inno_compiler_candidates_do_not_require_program_files_environment_variables(
    monkeypatch,
) -> None:
    builder = _load_builder()
    fallback = Path("C:/Program Files")
    monkeypatch.setattr(builder, "_registered_program_files_dirs", lambda: (fallback,))

    candidates = builder._candidate_iscc_paths({"PATH": ""})

    assert fallback / "Inno Setup 7" / "ISCC.exe" in candidates


def test_inno_definition_copies_the_whole_tree_without_hard_coded_workspace_paths() -> None:
    builder = _load_builder()
    text = builder.INNO_SOURCE.read_text(encoding="utf-8")

    assert "D:\\CodeSpace\\JoyRead" not in text
    assert 'Source: "{#MyAppSourceDir}\\*"' in text
    assert "recursesubdirs createallsubdirs" in text
    assert "#error MyAppSourceDir" in text
    assert (
        'Source: "{#MyProjectRoot}\\src\\joyread\\ui\\resources\\icons\\JoyReadDocument.ico"; '
        'DestDir: "{app}"; Flags: ignoreversion'
    ) in text


def test_inno_definition_matches_the_windows_shortcut_and_association_policy() -> None:
    builder = _load_builder()
    text = builder.INNO_SOURCE.read_text(encoding="utf-8")

    assert 'Name: "desktopicon"' in text
    desktop_line = next(line for line in text.splitlines() if 'Name: "desktopicon"' in line)
    assert "unchecked" not in desktop_line
    for task, message, extension in (
        ("openwith_cbz", "OpenWithCbz", ".cbz"),
        ("openwith_cbr", "OpenWithCbr", ".cbr"),
        ("openwith_cb7", "OpenWithCb7", ".cb7"),
        ("openwith_pdf", "OpenWithPdf", ".pdf"),
    ):
        task_line = next(line for line in text.splitlines() if f'Name: "{task}"' in line)
        assert "unchecked" in task_line
        assert f"{{cm:{message}}}" in task_line
        assert extension in next(
            line for line in text.splitlines() if line.startswith(f"english.{message}=")
        )
    assert "ChangesAssociations=yes" in text
    for extension in (".cbz", ".cbr", ".cb7", ".pdf"):
        assert f"Classes\\{extension}\\OpenWithProgids" in text
    for extension in (".zip", ".rar", ".7z"):
        assert f"Classes\\{extension}\\OpenWithProgids" in text
        generic_line = next(
            line for line in text.splitlines() if f'Classes\\{extension}\\OpenWithProgids' in line
        )
        assert "Tasks:" not in generic_line
    assert "Software\\Classes\\{#MyFileType}" in text
    assert "Software\\Classes\\Applications\\{#MyAppExeName}\\SupportedTypes" in text
    assert "Software\\JoyRead\\Capabilities" in text
    assert "Software\\RegisteredApplications" in text
    assert "uninsdeletekeyifempty" in text
    assert "Tasks: openwith\n" not in text
    assert (
        'Subkey: "Software\\Classes\\{#MyFileType}\\DefaultIcon"; '
        'ValueType: string; ValueData: "{app}\\JoyReadDocument.ico"'
    ) in text

    capability_lines = [
        line
        for line in text.splitlines()
        if "Software\\JoyRead\\Capabilities\\FileAssociations" in line
    ]
    assert all(extension not in "\n".join(capability_lines) for extension in (".zip", ".rar", ".7z"))


def test_inno_never_writes_an_extension_default_progid() -> None:
    builder = _load_builder()
    text = builder.INNO_SOURCE.read_text(encoding="utf-8")

    for extension in (".cbz", ".cbr", ".cb7", ".pdf", ".zip", ".rar", ".7z"):
        assert f'Subkey: "Software\\Classes\\{extension}";' not in text


def test_inno_rejects_a_missing_document_icon_source(tmp_path: Path) -> None:
    builder = _load_builder()

    with pytest.raises(SystemExit, match="Missing Windows file-association icon"):
        builder.validate_document_icon_source(tmp_path / "missing.ico")
