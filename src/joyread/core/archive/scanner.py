"""Recursive archive scanning and resource-aware page tree construction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from joyread.core.archive.errors import (
    ArchiveError,
    ArchivePasswordRejected,
    ArchivePasswordRequired,
    ArchiveResourceLimitError,
)
from joyread.core.archive.limits import ArchiveOpenLimits, ArchiveOperationBudget, ensure_item_size
from joyread.core.archive.models import ArchivePasswordPolicy, PasswordProvider
from joyread.core.archive.records import ArchiveListing, ArchiveSource, PageRecord
from joyread.core.archive.tree import (
    ArchiveTreeNode,
    ensure_folder_path,
    is_metadata_entry,
    safe_entry_name,
    strip_transparent_prefix,
    transparent_single_root_prefix,
    tree_has_pages,
)
from joyread.core.file_types import ARCHIVE_EXTENSIONS


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
# Bump when discovery or flattening semantics change. Extraction manifests
# include this version so records created under a previous scanner policy are
# never reused for a different page tree.
SCANNER_SCHEMA_VERSION = 1


@dataclass
class ArchiveScanContext:
    password_provider: PasswordProvider | None
    password_policy: ArchivePasswordPolicy
    skipped_archives: set[str]
    limits: ArchiveOpenLimits
    budget: ArchiveOperationBudget


class ArchiveSourceSkipped(Exception):
    """Internal control flow for a user-skipped encrypted archive source."""


ListEntries = Callable[[ArchiveSource, ArchiveScanContext], ArchiveListing]
ReadEntry = Callable[
    [ArchiveSource, str, str | None],
    bytes,
]


class ArchiveScanner:
    """Build one logical archive tree while preserving one scan byte budget."""

    def __init__(
        self,
        list_entries: ListEntries,
        read_entry: Callable[..., bytes],
    ) -> None:
        self._list_entries = list_entries
        self._read_entry = read_entry

    def scan(
        self,
        source: ArchiveSource,
        context: ArchiveScanContext,
        *,
        nested_depth: int = 0,
        global_base_depth: int = 0,
    ) -> ArchiveTreeNode:
        """Discover images and nested archives in deterministic DFS order.

        The context's operation budget is shared by every nested archive, so
        a deeply nested input cannot reset the configured scan allowance.
        """

        try:
            listing = self._list_entries(source, context)
        except ArchiveSourceSkipped:
            return ArchiveTreeNode("", "", "root")
        source = replace(
            source,
            requires_sequential_warmup=listing.requires_sequential_warmup,
        )
        entries = listing.entries
        entry_prefix = transparent_single_root_prefix(entries)
        root = ArchiveTreeNode("", "", "root")

        for entry in entries:
            safe_name = safe_entry_name(entry.name)
            if safe_name is None or is_metadata_entry(safe_name):
                continue

            logical_name = strip_transparent_prefix(safe_name, entry_prefix)
            if logical_name is None:
                continue
            suffix = PurePosixPath(logical_name).suffix.lower()
            physical_parts = PurePosixPath(safe_name).parts
            folder_depth = max(0, len(physical_parts) - 1)
            global_entry_depth = global_base_depth + folder_depth
            if suffix in IMAGE_EXTENSIONS:
                if not _depth_is_allowed(global_entry_depth, context.limits.global_file_max_depth):
                    continue
                ensure_item_size(entry.size, context.limits.max_extracted_item_bytes, logical_name)
                parent = ensure_folder_path(root, PurePosixPath(logical_name).parts[:-1])
                parent.pages.append(self._page_record(source, safe_name, logical_name, entry))
                continue

            next_nested_depth = nested_depth + 1
            nested_global_depth = global_entry_depth + 1
            if (
                suffix not in ARCHIVE_EXTENSIONS
                or not _depth_is_allowed(next_nested_depth, context.limits.nested_archive_max_depth)
                or not _depth_is_allowed(nested_global_depth, context.limits.global_file_max_depth)
            ):
                continue
            try:
                ensure_item_size(entry.size, context.limits.max_extracted_item_bytes, logical_name)
                nested_data = self._read_entry(
                    source,
                    safe_name,
                    entry.password,
                    limits=context.limits,
                    budget=context.budget,
                )
                nested_source = ArchiveSource(
                    label=f"{source.label}::{logical_name}",
                    suffix=suffix,
                    data=nested_data,
                    allow_persistent_cache=source.allow_persistent_cache and entry.password is None,
                )
                nested_root = self.scan(
                    nested_source,
                    context,
                    nested_depth=next_nested_depth,
                    global_base_depth=nested_global_depth,
                )
            except (ArchivePasswordRequired, ArchivePasswordRejected):
                raise
            except ArchiveResourceLimitError:
                raise
            except ArchiveError:
                # A malformed nested archive is intentionally skipped, but a
                # configured resource limit is a user-visible controlled
                # failure and must never be silently converted to an omission.
                continue
            if tree_has_pages(nested_root):
                logical_path = PurePosixPath(logical_name)
                parent = ensure_folder_path(root, logical_path.parts[:-1])
                parent.children.append(
                    ArchiveTreeNode(
                        name=logical_path.name,
                        label=logical_path.stem,
                        kind="archive",
                        pages=nested_root.pages,
                        children=nested_root.children,
                        folders=nested_root.folders,
                    )
                )

        return root

    @staticmethod
    def _page_record(
        source: ArchiveSource,
        name: str,
        logical_name: str,
        entry: ArchiveEntry,
    ) -> PageRecord:
        return PageRecord(
            display_path=f"{source.label}/{logical_name}",
            source=source,
            name=name,
            password=entry.password,
            size=entry.size,
        )


def _depth_is_allowed(depth: int, maximum: int | None) -> bool:
    return maximum is None or depth <= maximum
