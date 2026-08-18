import tomllib

from PySide6.QtGui import QIcon

from joyread.infrastructure.resources.resource_loader import ResourceLoader


def test_app_icon_resource_is_available(qtbot) -> None:
    loader = ResourceLoader()
    path = loader.app_icon_path()
    icon = QIcon(str(path))

    assert path.name == "JoyRead.icns"
    assert path.exists()
    assert not icon.isNull()
    assert any(size.width() == 1024 and size.height() == 1024 for size in icon.availableSizes())


def test_app_icon_uses_single_canonical_resource() -> None:
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
