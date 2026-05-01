from pathlib import Path

from joyread.infrastructure.filesystem.path_service import PathService, WritableLocation


def test_path_service_resolves_required_directories_under_override(tmp_path: Path) -> None:
    base_dir = tmp_path / "joyread-runtime"
    service = PathService(base_dir=base_dir)

    paths = service.paths.as_dict()

    assert set(paths) == set(WritableLocation)
    for directory in paths.values():
        assert directory.is_relative_to(base_dir)
        assert "JoyRead" not in directory.parts[:-1]


def test_path_service_creates_directories(tmp_path: Path) -> None:
    service = PathService(base_dir=tmp_path / "runtime")

    service.ensure_directories()

    assert all(path.is_dir() for path in service.required_directories())
