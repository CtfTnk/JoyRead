from pathlib import Path

import pytest

from joyread.infrastructure.filesystem.path_service import (
    PathService,
    StoragePathResolver,
    WritableLocation,
)


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


def test_base_dir_layout_is_flat_under_root(tmp_path: Path) -> None:
    # The base_dir/test layout mirrors production: Books/Cache/Thumbnails sit
    # directly under the storage root, never nested under a Data/ or Cache/ dir.
    service = PathService(base_dir=tmp_path)

    assert service.storage_root == tmp_path.resolve()
    assert service.paths.books == tmp_path / "Books"
    assert service.paths.cache == tmp_path / "Cache"
    assert service.paths.thumbnails == tmp_path / "Thumbnails"
    assert service.paths.database == tmp_path / "Database"


def test_storage_root_matches_storage_root_mode(tmp_path: Path) -> None:
    service = PathService(storage_root=tmp_path / "lib", support_root=tmp_path / "support")

    assert service.storage_root == (tmp_path / "lib").resolve()
    assert service.resolver.storage_root == (tmp_path / "lib").resolve()


def test_resolve_builds_absolute_under_location(tmp_path: Path) -> None:
    service = PathService(base_dir=tmp_path)

    resolved = service.resolve(WritableLocation.CACHE, ".archive_zip_bundles")

    assert resolved == service.paths.cache / ".archive_zip_bundles"


class TestStoragePathResolver:
    def _resolver(self, tmp_path: Path) -> StoragePathResolver:
        return StoragePathResolver(tmp_path)

    def test_absolute_under_root_becomes_relative_posix(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)
        absolute = tmp_path / "Books" / "ab" / "hash.cbz"

        assert resolver.to_storage_relative(absolute) == "Books/ab/hash.cbz"

    def test_relative_resolves_back_to_absolute_under_root(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)

        assert resolver.to_storage_absolute("Books/ab/hash.cbz") == (
            tmp_path / "Books" / "ab" / "hash.cbz"
        ).resolve()

    def test_round_trip(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)
        original = tmp_path / "Thumbnails" / "covers" / "x-custom-200x300.png"

        relative = resolver.to_storage_relative(original)
        assert resolver.to_storage_absolute(relative) == original.resolve()

    def test_path_outside_root_is_rejected(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)
        outside = tmp_path.parent / "elsewhere" / "file.cbz"

        with pytest.raises(ValueError):
            resolver.to_storage_relative(outside)

    @pytest.mark.parametrize("evil", ["../escape.cbz", "Books/../../etc/passwd", "a/../../b"])
    def test_parent_traversal_is_rejected(self, tmp_path: Path, evil: str) -> None:
        resolver = self._resolver(tmp_path)

        with pytest.raises(ValueError):
            resolver.to_storage_absolute(evil)

    def test_absolute_relative_value_is_rejected(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)

        with pytest.raises(ValueError):
            resolver.to_storage_absolute("/etc/passwd")

    def test_is_managed(self, tmp_path: Path) -> None:
        resolver = self._resolver(tmp_path)

        assert resolver.is_managed(tmp_path / "Books" / "x.cbz")
        assert not resolver.is_managed(tmp_path.parent / "other" / "x.cbz")
