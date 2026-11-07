"""Where a managed file lives under ``Books/``, and why the two kinds differ.

**One row of ``book_files`` owns exactly one file.** No two rows ever share a
physical artifact. Everything downstream depends on it: deleting a book unlinks
its file without counting references, and library maintenance merges two rows
whose bytes match by deleting one of them.

Verbatim files satisfy that by content addressing, and provably so:
``stored_hash`` equals ``source_hash`` for them, and ``source_hash`` is UNIQUE,
so two verbatim rows cannot name the same path.

Canonical files cannot. Two genuinely different sources -- a ``.cbr`` and a
``.7z`` holding the same pages -- repackage deterministically to *identical*
bytes, so their ``stored_hash`` collides while their ``source_hash`` does not.
Content addressing would hand both rows one file; deleting either book would
then delete the other's pages, and maintenance would "merge" them, discarding a
distinct source identity and letting that source re-import as new.

So canonical artifacts are addressed by ``file_id`` instead. The cost is one
duplicated file in a rare case; the benefit is that the one-row-one-file
invariant stays true for every row, and nothing downstream needs to learn about
sharing.

Sealed files (Hidden Space, ``joyread.core.vault``) are addressed by
``file_id`` for a stronger reason: their bytes are fresh random ciphertext on
every seal, so a content address would change each time the same book is
sealed, and equal hashes can never mean equal books.
"""

from __future__ import annotations

from pathlib import Path


#: The stored bytes are the user's own file, copied unchanged.
STORAGE_KIND_VERBATIM = "verbatim"

#: JoyRead repackaged the source into its canonical container.
STORAGE_KIND_CANONICAL = "canonical"

#: The content is sealed in a ``.jrl`` to the Library's vault key. The stored
#: hash covers the ciphertext, so integrity checks work while the vault is
#: locked.
STORAGE_KIND_SEALED = "sealed"

STORAGE_KINDS = frozenset({STORAGE_KIND_VERBATIM, STORAGE_KIND_CANONICAL, STORAGE_KIND_SEALED})


def storage_target(
    books_root: Path,
    *,
    storage_kind: str,
    stored_hash: str,
    file_id: str,
    suffix: str,
) -> Path:
    """The one path a row's artifact belongs at.

    Import and library maintenance must agree on this or maintenance will
    "repair" a healthy book by moving it somewhere import would never look.
    """

    if not is_content_addressed(storage_kind):
        return books_root / file_id[:2] / f"{file_id}{suffix}"
    return books_root / stored_hash[:2] / f"{stored_hash}{suffix}"


def is_content_addressed(storage_kind: str) -> bool:
    """Whether the artifact's path is derived from its bytes.

    Only content-addressed rows may be relocated when their hash changes, or
    merged with another row that hashes the same. That is verbatim rows only;
    a kind this code does not recognize is never assumed to qualify, because
    a wrong "yes" lets maintenance merge two books into one.
    """

    return storage_kind == STORAGE_KIND_VERBATIM


__all__ = [
    "STORAGE_KINDS",
    "STORAGE_KIND_CANONICAL",
    "STORAGE_KIND_SEALED",
    "STORAGE_KIND_VERBATIM",
    "is_content_addressed",
    "storage_target",
]
