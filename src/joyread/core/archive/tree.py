"""Archive directory tree construction, natural ordering, and Contents flattening."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from natsort import natsort_keygen, ns

from joyread.core.archive.models import ArchiveContentsEntry
from joyread.core.archive.records import ArchiveEntry, PageRecord


_NATURAL_KEY = natsort_keygen(alg=ns.INT | ns.IGNORECASE | ns.PRESORT)


@dataclass
class ArchiveTreeNode:
    """Logical folder/archive node used to derive pages and Contents together."""

    name: str
    label: str
    kind: str
    pages: list[PageRecord] = field(default_factory=list)
    children: list["ArchiveTreeNode"] = field(default_factory=list)
    folders: dict[str, "ArchiveTreeNode"] = field(default_factory=dict)


def safe_entry_name(name: str) -> str | None:
    """Return a normalized archive member path or reject unsafe traversal."""

    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def is_junk_entry(name: str) -> bool:
    """Filesystem noise an archiver added, never content the user put there.

    Named for what it matches: ``__MACOSX`` sidecars, ``.DS_Store``, and the
    ``._`` resource forks macOS writes beside real files. Deliberately *not*
    document metadata -- ``ComicInfo.xml`` and ``meta.json`` are content this
    app reads, and the previous name (``is_metadata_entry``) implied they were
    filtered here.
    """

    parts = PurePosixPath(name).parts
    if not parts:
        return True
    if parts[0] == "__MACOSX":
        return True
    return any(part == ".DS_Store" or part.startswith("._") for part in parts)


def transparent_single_root_prefix(entries: Iterable[ArchiveEntry]) -> str | None:
    roots: set[str] = set()
    for entry in entries:
        safe_name = safe_entry_name(entry.name)
        if safe_name is None or is_junk_entry(safe_name):
            continue
        parts = PurePosixPath(safe_name).parts
        if len(parts) <= 1:
            return None
        roots.add(parts[0])
        if len(roots) > 1:
            return None
    if len(roots) == 1:
        return next(iter(roots))
    return None


def strip_transparent_prefix(name: str, prefix: str | None) -> str | None:
    if prefix is None:
        return name
    parts = PurePosixPath(name).parts
    if len(parts) <= 1 or parts[0] != prefix:
        return name
    stripped = PurePosixPath(*parts[1:]).as_posix()
    return stripped or None


def ensure_folder_path(root: ArchiveTreeNode, parts: Sequence[str]) -> ArchiveTreeNode:
    node = root
    for part in parts:
        child = node.folders.get(part)
        if child is None:
            child = ArchiveTreeNode(part, part, "folder")
            node.folders[part] = child
            node.children.append(child)
        node = child
    return node


def tree_has_pages(node: ArchiveTreeNode) -> bool:
    # Nodes are only attached after an accepted page or non-empty nested
    # archive is found, so a child is itself proof of a page-bearing subtree.
    return bool(node.pages or node.children)


def disambiguate_nested_archive_labels(node: ArchiveTreeNode) -> None:
    pending = [node]
    while pending:
        current = pending.pop()
        visible_children = [child for child in current.children if tree_has_pages(child)]
        label_counts: dict[str, int] = {}
        for child in visible_children:
            key = child.label.casefold()
            label_counts[key] = label_counts.get(key, 0) + 1
        for child in visible_children:
            if child.kind == "archive" and label_counts.get(child.label.casefold(), 0) > 1:
                child.label = child.name
        pending.extend(visible_children)


def flatten_archive_tree(root: ArchiveTreeNode) -> tuple[list[PageRecord], tuple[ArchiveContentsEntry, ...]]:
    """Flatten direct pages then naturally sorted children in DFS pre-order."""

    pages: list[PageRecord] = []
    contents: list[ArchiveContentsEntry] = []

    pages.extend(_sorted_pages(root.pages))
    pending = [(child, 0) for child in reversed(_sorted_children(root.children))]
    while pending:
        node, depth = pending.pop()
        contents.append(ArchiveContentsEntry(node.label, len(pages), depth))
        pages.extend(_sorted_pages(node.pages))
        children = _sorted_children(node.children)
        pending.extend((child, depth + 1) for child in reversed(children))
    return pages, tuple(contents)


def flatten_archive_tree_for_writing(
    root: ArchiveTreeNode,
) -> list[tuple[str, PageRecord]]:
    """The same pages in the same order, each with the folder it belongs in.

    :func:`flatten_archive_tree` throws the tree shape away once it has the
    reading order, which is all a reader needs. Writing a canonical archive
    needs the shape back: every node becomes a real directory, so that
    re-scanning the result rebuilds an identical tree and therefore an identical
    table of contents.

    Deliberately shares ``_sorted_pages`` / ``_sorted_children`` with the
    flattener rather than re-deriving an order. Two orderings that are meant to
    agree but are written twice will eventually disagree.
    """

    placed: list[tuple[str, PageRecord]] = [("", page) for page in _sorted_pages(root.pages)]
    pending = [(child, "") for child in reversed(_sorted_children(root.children))]
    while pending:
        node, prefix = pending.pop()
        # The node's *label* names the directory, not its ``name``: the label is
        # what a reader shows in Contents, and the scanner derives a folder's
        # label from its directory name -- so writing the label back out is what
        # makes the round trip identity-preserving.
        node_prefix = f"{prefix}{node.label}/"
        placed.extend((node_prefix, page) for page in _sorted_pages(node.pages))
        pending.extend(
            (child, node_prefix) for child in reversed(_sorted_children(node.children))
        )
    return placed


def _sorted_pages(pages: Iterable[PageRecord]) -> list[PageRecord]:
    return sorted(
        pages,
        key=lambda page: (
            _NATURAL_KEY(PurePosixPath(page.display_path).name),
            page.display_path.casefold(),
        ),
    )


def _sorted_children(children: Iterable[ArchiveTreeNode]) -> list[ArchiveTreeNode]:
    return sorted(
        (child for child in children if tree_has_pages(child)),
        key=lambda child: (
            _NATURAL_KEY(child.label),
            _NATURAL_KEY(child.name),
            child.kind,
        ),
    )
