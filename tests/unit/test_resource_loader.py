import sys
import tomllib

from PIL import Image
from PySide6.QtGui import QIcon
import pytest

from joyread.infrastructure.resources.resource_loader import ResourceLoader


def test_app_icon_resource_is_available(qtbot) -> None:  # noqa: ARG001
    loader = ResourceLoader()
    path = loader.app_icon_path()
    icon = QIcon(str(path))

    assert path.exists()
    assert not icon.isNull()
    assert icon.availableSizes()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", "JoyRead.ico"),
        ("darwin", "JoyRead.icns"),
        ("linux", "JoyRead.png"),
        # Anything unrecognized gets the portable format rather than a macOS
        # container no other desktop reads.
        ("freebsd14", "JoyRead.png"),
    ],
)
def test_each_platform_gets_its_native_icon_container(
    platform: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3.76 MB .icns costs 53-69 ms to decode and 2-3 ms for the .ico.

    Serving one format everywhere spent that on every Windows and Linux launch
    to render an icon those platforms cannot use.
    """

    monkeypatch.setattr(sys, "platform", platform)
    path = ResourceLoader().app_icon_path()

    assert path.name == expected
    assert path.is_file()


def test_a_missing_platform_icon_falls_back_instead_of_vanishing(
    tmp_path,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path that does not exist yields a silently empty QIcon.

    That reads as a design choice rather than a packaging bug, so the loader
    substitutes a bundled format and says so.
    """

    icons = tmp_path / "ui" / "resources" / "icons"
    icons.mkdir(parents=True)
    (icons / "JoyRead.png").write_bytes(b"stand-in")
    monkeypatch.setattr(sys, "platform", "win32")

    path = ResourceLoader(package_root=tmp_path).app_icon_path()

    assert path.name == "JoyRead.png"


def test_linux_app_icon_is_a_usable_raster() -> None:
    path = ResourceLoader().icon_path("JoyRead.png")

    with Image.open(path) as icon:
        assert icon.format == "PNG"
        assert icon.mode == "RGBA"
        # 512 is the largest size the .icns carries as a distinct entry and the
        # largest hicolor size Linux desktops install.
        assert icon.size == (512, 512)


def test_windows_app_icon_has_native_multisize_resource() -> None:
    path = ResourceLoader().icon_path("JoyRead.ico")

    with Image.open(path) as icon:
        assert icon.format == "ICO"
        assert icon.mode == "RGBA"
        assert icon.info["sizes"] == {
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


def test_app_icon_has_no_legacy_duplicates() -> None:
    icon_dir = ResourceLoader().icon_path("")

    assert not (icon_dir / "joyread_app_icon.icns").exists()
    assert not (icon_dir / "joyread_app_icon.iconset").exists()
    assert not (icon_dir / "icon_app.png").exists()


def test_noto_font_resources_are_available() -> None:
    loader = ResourceLoader()
    font_paths = loader.font_paths()
    font_dir = loader.font_path("")

    assert {path.name for path in font_paths} == {
        "NotoSansSC-Regular.otf",
        "NotoSansSC-Bold.otf",
        "NotoSansJP-Regular.otf",
        "NotoSansJP-Bold.otf",
    }
    assert all(path.exists() for path in font_paths)
    assert (font_dir / "OFL.txt").exists()


def test_locale_resources_are_available_and_packaged() -> None:
    loader = ResourceLoader()
    locale_dir = loader.locale_dir()

    assert locale_dir.exists()
    assert {path.name for path in locale_dir.glob("*.json")} >= {"en.json", "zh.json", "ja.json"}

    pyproject = tomllib.loads((loader.package_root.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["joyread"]
    assert "resources/locales/*.json" in package_data


def test_stylesheet_uses_noto_font_stack() -> None:
    stylesheet = ResourceLoader().load_stylesheet()

    assert "__FONT_FAMILY__" not in stylesheet
    assert "Inter" not in stylesheet
    assert "Noto Sans SC" in stylesheet
    assert "Noto Sans JP" in stylesheet
