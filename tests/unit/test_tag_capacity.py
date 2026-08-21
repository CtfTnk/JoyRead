"""The per-library tag cap.

``MAX_TAG_COUNT`` is a performance ceiling, not a storage one: every tag
surface builds one chip widget per tag, so a library past a few thousand tags
makes the tag dialogs slow rather than merely large. These tests pin the
boundary and, more importantly, pin what the cap must *not* block.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from joyread.core.models.tag import MAX_TAG_COUNT, Tag, normalized_tag_key
from joyread.core.repositories.sqlite_tag_repository import SqliteTagRepository
from joyread.core.services.tag_service import TagService
from joyread.infrastructure.database import (
    DatabaseInterpreter,
    DatabasePriority,
    apply_migrations,
)
from tests.unit.test_tag_management_page import _FakeRepository


def _service_with(count: int) -> TagService:
    repo = _FakeRepository()
    for index in range(count):
        repo.create(f"Tag{index}")
    return TagService(repo)


def test_a_library_below_the_cap_can_still_create() -> None:
    service = _service_with(3)

    tag = service.create("Fresh")

    assert tag.name == "Fresh"
    assert not service.at_capacity


def test_create_is_refused_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the ceiling rather than building 5,000 tags: the rule under test
    # is "refuse at the limit", not the specific number.
    monkeypatch.setattr("joyread.core.services.tag_service.MAX_TAG_COUNT", 3)
    service = _service_with(3)

    assert service.at_capacity
    with pytest.raises(ValueError) as excinfo:
        service.create("One too many")

    # The Settings page shows this string verbatim, so it has to say what to
    # do about it, not just that something failed.
    assert "maximum" in str(excinfo.value)
    assert "Delete a tag" in str(excinfo.value)


def test_reusing_an_existing_tag_still_works_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap bounds how many tags exist, not how many books carry one.

    Import calls ``find_or_create`` for every tag on every book; if that were
    refused at the cap, a full library would stop being able to tag imports
    with tags it already has.
    """

    monkeypatch.setattr("joyread.core.services.tag_service.MAX_TAG_COUNT", 3)
    service = _service_with(3)

    reused = service.find_or_create("Tag1")

    assert reused is not None
    assert reused.name_normalized == normalized_tag_key("Tag1")


def test_import_skips_a_new_tag_at_the_cap_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_or_create`` returns None the same way it does for an invalid
    name, so one refused tag does not abort the book being imported."""

    monkeypatch.setattr("joyread.core.services.tag_service.MAX_TAG_COUNT", 3)
    service = _service_with(3)

    assert service.find_or_create("Brand New Tag") is None


def test_concurrent_manual_and_import_creation_cannot_cross_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repository actor must check the count and insert in one callback.

    A service-level pre-check lets the higher-priority reads from a manual
    create and an import both observe free capacity before either queued write
    runs, which creates two rows at a cap of one.
    """

    monkeypatch.setattr("joyread.core.services.tag_service.MAX_TAG_COUNT", 1)
    database = DatabaseInterpreter(tmp_path / "joyread.sqlite3")
    database.execute(apply_migrations, DatabasePriority.CRITICAL)
    repository = SqliteTagRepository(database)
    service = TagService(repository)
    start = Barrier(2)

    def create_manually() -> Tag | None:
        start.wait()
        try:
            return service.create("Manual")
        except ValueError:
            return None

    def create_from_import() -> Tag | None:
        start.wait()
        return service.find_or_create("Imported")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            manual_future = executor.submit(create_manually)
            import_future = executor.submit(create_from_import)
            outcomes = (manual_future.result(), import_future.result())
        tags = repository.list_tags()
    finally:
        database.close()

    assert sum(outcome is not None for outcome in outcomes) == 1
    assert len(tags) == 1


def test_the_shipped_cap_matches_the_measured_ceiling() -> None:
    """5,000 is the last scale where a tag dialog still opens in under a
    second: 891ms to open and 282ms per search, against 1.8s and 587ms at
    10,000. See ``MAX_TAG_COUNT`` and docs/technical/runtime-flows.md."""

    assert MAX_TAG_COUNT == 5000


def test_normalized_key_matches_the_model_property() -> None:
    """Two definitions of "same tag" would let the cap's existence check
    disagree with the repository's uniqueness check."""

    for name in ("Manga", "  manga  ", "MANGA", "ホラー"):
        assert normalized_tag_key(name) == Tag("id", name).name_normalized
