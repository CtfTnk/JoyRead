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
