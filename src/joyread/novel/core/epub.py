"""MIT-safe EPUB parser built on stdlib ``zipfile`` and ``lxml``.

The module deliberately avoids ``ebooklib`` (AGPL-3.0). All XML
parsing goes through ``lxml.etree`` (BSD-3-Clause), and asset bytes
are fetched lazily through the :class:`EpubAssetReader` protocol so
a future encryption layer can slot in without touching the engine.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lxml import etree


logger = logging.getLogger(__name__)


_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_EPUB_NS = "http://www.idpf.org/2007/ops"


# Exceptions ---------------------------------------------------------------


class EpubError(Exception):
    """Base class for all EPUB-related errors."""


class InvalidEpubError(EpubError):
    """Raised when the file is malformed enough that we cannot continue."""


class EpubPasswordRequired(EpubError):
    """Raised by an encrypted ``EpubAssetReader`` when no password is supplied
    or the supplied one fails decryption. Future encryption work raises this;
    the unencrypted default reader never does.
    """

    def __init__(self, source_path: Path, *, is_retry: bool = False) -> None:
        super().__init__(f"Encrypted EPUB requires a password: {source_path}")
        self.source_path = source_path
        self.is_retry = is_retry


# Asset reader protocol (encryption seam) ----------------------------------


class EpubAssetReader(Protocol):
    """Reads bytes for an asset path relative to the EPUB root.

    A real ZIP-backed implementation reads straight from the archive.
    A future encrypted variant wraps the default and decrypts before
    returning. The engine itself does not know which it is talking to.
    """

    def read(self, path: str) -> bytes: ...

    def exists(self, path: str) -> bool: ...

    def close(self) -> None: ...


class ZipFileAssetReader:
    """Default ``EpubAssetReader`` backed by ``zipfile.ZipFile``."""

    def __init__(self, source_path: Path) -> None:
        self._source_path = Path(source_path)
        self._zip = zipfile.ZipFile(self._source_path, mode="r")
        # Cache the name set so ``exists`` is O(1) and case-correct against
        # the original archive (Qt's loadResource calls are case-sensitive).
        self._names = frozenset(self._zip.namelist())

    @property
    def source_path(self) -> Path:
        return self._source_path

    def read(self, path: str) -> bytes:
        normalized = path.lstrip("/")
        with self._zip.open(normalized, mode="r") as handle:
            return handle.read()

    def exists(self, path: str) -> bool:
        return path.lstrip("/") in self._names

    def close(self) -> None:
        self._zip.close()


# Data model ---------------------------------------------------------------


@dataclass(frozen=True)
class EpubMetadata:
    title: str
    creators: tuple[str, ...]
    language: str | None
    identifier: str | None
    cover_href: str | None
    primary_writing_mode: str | None  # "horizontal-tb", "vertical-rl", etc.


@dataclass(frozen=True)
class EpubManifestItem:
    item_id: str
    href: str  # archive-relative (resolved from OPF dir)
    media_type: str
    properties: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class EpubSpineItem:
    item_id: str
    href: str
    media_type: str


@dataclass(frozen=True)
class EpubTocItem:
    label: str
    href: str  # archive-relative, may include ``#anchor``
    spine_index: int  # -1 if not in spine
    anchor_id: str | None
    depth: int
    children: tuple["EpubTocItem", ...] = ()


@dataclass(frozen=True)
class EpubBook:
    metadata: EpubMetadata
    manifest: dict[str, EpubManifestItem]
    spine: tuple[EpubSpineItem, ...]
    toc: tuple[EpubTocItem, ...]
    base_dir: str  # OPF parent dir, archive-relative (no leading slash)
    opf_path: str

    def spine_index_for_href(self, href: str) -> int:
        """Return the spine index whose href matches, or -1 if absent.

        Strips anchor fragments. Matches against archive-relative paths
        normalised through ``posixpath`` so ``a/b/../c.xhtml`` and
        ``a/c.xhtml`` compare equal.
        """
        target = posixpath.normpath((href.split("#", 1)[0] or "").lstrip("/"))
        for index, item in enumerate(self.spine):
            if posixpath.normpath(item.href.lstrip("/")) == target:
                return index
        return -1


# Parser -------------------------------------------------------------------


def open_epub(
    source_path: Path | str,
    *,
    asset_reader_factory=None,
) -> tuple[EpubBook, EpubAssetReader]:
    """Open an EPUB file and parse metadata, manifest, spine, TOC.

    Returns the parsed :class:`EpubBook` plus the ``EpubAssetReader`` that
    owns the underlying ZIP handle. Callers must call ``reader.close()``
    when done (or use the :class:`EpubReaderSession` wrapper).

    ``asset_reader_factory`` is a hook for future encrypted-reader
    injection — by default a :class:`ZipFileAssetReader` is created.
    """
    path = Path(source_path)
    reader: EpubAssetReader
    if asset_reader_factory is None:
        reader = ZipFileAssetReader(path)
    else:
        reader = asset_reader_factory(path)
    try:
        opf_path = _resolve_opf_path(reader)
        opf_bytes = reader.read(opf_path)
        opf_root = _parse_xml(opf_bytes, opf_path)
        base_dir = posixpath.dirname(opf_path)

        metadata = _parse_metadata(opf_root, base_dir)
        manifest = _parse_manifest(opf_root, base_dir)
        spine = _parse_spine(opf_root, manifest)
        toc = _parse_toc(opf_root, manifest, base_dir, spine, reader)

        book = EpubBook(
            metadata=metadata,
            manifest=manifest,
            spine=spine,
            toc=toc,
            base_dir=base_dir,
            opf_path=opf_path,
        )
        logger.info(
            "epub opened: path=%s title=%r spine=%d toc=%d",
            path,
            metadata.title,
            len(spine),
            len(toc),
        )
        return book, reader
    except Exception:
        # Don't leak the ZIP handle if parsing fails midway through.
        reader.close()
        raise


def _parse_xml(data: bytes, path_for_error: str) -> etree._Element:
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise InvalidEpubError(f"Malformed XML in {path_for_error}: {exc}") from exc


def _resolve_opf_path(reader: EpubAssetReader) -> str:
    container_path = "META-INF/container.xml"
    if not reader.exists(container_path):
        raise InvalidEpubError("EPUB is missing META-INF/container.xml")
    root = _parse_xml(reader.read(container_path), container_path)
    rootfile = root.find(f"{{{_CONTAINER_NS}}}rootfiles/{{{_CONTAINER_NS}}}rootfile")
    if rootfile is None or "full-path" not in rootfile.attrib:
        raise InvalidEpubError("container.xml has no <rootfile full-path>")
    return rootfile.attrib["full-path"]


def _resolve_href(base_dir: str, href: str) -> str:
    """Resolve a manifest/spine href relative to the OPF directory.

    The result is an archive-relative POSIX path (no leading slash).
    """
    if not href:
        return ""
    if base_dir:
        joined = posixpath.normpath(posixpath.join(base_dir, href))
    else:
        joined = posixpath.normpath(href)
    return joined.lstrip("./")


def _text_or_none(element: etree._Element | None) -> str | None:
    if element is None:
        return None
    text = element.text
    return text.strip() if text else None


def _parse_metadata(opf_root: etree._Element, base_dir: str) -> EpubMetadata:
    metadata_el = opf_root.find(f"{{{_OPF_NS}}}metadata")
    title = "Untitled"
    creators: list[str] = []
    language: str | None = None
    identifier: str | None = None
    cover_href: str | None = None
    primary_writing_mode: str | None = None

    if metadata_el is not None:
        title_el = metadata_el.find(f"{{{_DC_NS}}}title")
        title = _text_or_none(title_el) or "Untitled"
        for creator in metadata_el.findall(f"{{{_DC_NS}}}creator"):
            name = _text_or_none(creator)
            if name:
                creators.append(name)
        language = _text_or_none(metadata_el.find(f"{{{_DC_NS}}}language"))
        identifier = _text_or_none(metadata_el.find(f"{{{_DC_NS}}}identifier"))
        for meta in metadata_el.findall(f"{{{_OPF_NS}}}meta"):
            name = meta.attrib.get("name")
            content = meta.attrib.get("content")
            if name == "primary-writing-mode" and content:
                primary_writing_mode = content
            # EPUB 2 cover discovery: <meta name="cover" content="<item-id>"/>.
            if name == "cover" and content:
                cover_href = content  # resolved below against manifest

    # Resolve cover_href via manifest if it was an item id.
    manifest_el = opf_root.find(f"{{{_OPF_NS}}}manifest")
    if manifest_el is not None:
        # EPUB 3 cover: <item properties="cover-image">.
        if not cover_href:
            for item in manifest_el.findall(f"{{{_OPF_NS}}}item"):
                if "cover-image" in (item.attrib.get("properties") or "").split():
                    cover_href = item.attrib.get("href")
                    break
        # Resolve EPUB 2 id-based cover.
        if cover_href:
            for item in manifest_el.findall(f"{{{_OPF_NS}}}item"):
                if item.attrib.get("id") == cover_href and item.attrib.get("href"):
                    cover_href = item.attrib["href"]
                    break
        if cover_href:
            cover_href = _resolve_href(base_dir, cover_href)

    return EpubMetadata(
        title=title,
        creators=tuple(creators),
        language=language,
        identifier=identifier,
        cover_href=cover_href,
        primary_writing_mode=primary_writing_mode,
    )


def _parse_manifest(opf_root: etree._Element, base_dir: str) -> dict[str, EpubManifestItem]:
    manifest_el = opf_root.find(f"{{{_OPF_NS}}}manifest")
    if manifest_el is None:
        raise InvalidEpubError("OPF has no <manifest>")
    items: dict[str, EpubManifestItem] = {}
    for item in manifest_el.findall(f"{{{_OPF_NS}}}item"):
        item_id = item.attrib.get("id")
        href = item.attrib.get("href")
        media_type = item.attrib.get("media-type", "")
        if not item_id or not href:
            continue
        properties = frozenset((item.attrib.get("properties") or "").split())
        items[item_id] = EpubManifestItem(
            item_id=item_id,
            href=_resolve_href(base_dir, href),
            media_type=media_type,
            properties=properties,
        )
    return items


def _parse_spine(
    opf_root: etree._Element,
    manifest: dict[str, EpubManifestItem],
) -> tuple[EpubSpineItem, ...]:
    spine_el = opf_root.find(f"{{{_OPF_NS}}}spine")
    if spine_el is None:
        raise InvalidEpubError("OPF has no <spine>")
    spine: list[EpubSpineItem] = []
    for itemref in spine_el.findall(f"{{{_OPF_NS}}}itemref"):
        idref = itemref.attrib.get("idref")
        if not idref:
            continue
        item = manifest.get(idref)
        if item is None:
            logger.warning("spine references missing manifest id %r; skipping", idref)
            continue
        # Filter out non-readable items defensively (the spine usually only
        # contains XHTML but some malformed EPUBs include images).
        if item.media_type and "xhtml" not in item.media_type and "html" not in item.media_type:
            logger.debug(
                "spine item %r media-type=%s is not XHTML; keeping anyway",
                idref,
                item.media_type,
            )
        spine.append(
            EpubSpineItem(item_id=item.item_id, href=item.href, media_type=item.media_type)
        )
    if not spine:
        raise InvalidEpubError("OPF spine is empty")
    return tuple(spine)


def _parse_toc(
    opf_root: etree._Element,
    manifest: dict[str, EpubManifestItem],
    base_dir: str,
    spine: tuple[EpubSpineItem, ...],
    reader: EpubAssetReader,
) -> tuple[EpubTocItem, ...]:
    """Find and parse the TOC. Prefers EPUB 3 nav; falls back to NCX.

    Returns a flat-ish tuple of top-level entries with nested children
    populated under ``EpubTocItem.children``. Spine indices are resolved
    so the UI can seek without consulting the spine again.
    """
    # EPUB 3 nav: manifest item with properties="nav".
    nav_item: EpubManifestItem | None = None
    for item in manifest.values():
        if "nav" in item.properties:
            nav_item = item
            break
    if nav_item is not None and reader.exists(nav_item.href):
        try:
            return _parse_nav(reader.read(nav_item.href), nav_item.href, spine)
        except Exception as exc:
            logger.warning("EPUB 3 nav parse failed (%s); falling back to NCX", exc)

    # EPUB 2 fallback: <spine toc="ncx-item-id"> → manifest item.
    spine_el = opf_root.find(f"{{{_OPF_NS}}}spine")
    ncx_id = spine_el.attrib.get("toc") if spine_el is not None else None
    ncx_item: EpubManifestItem | None = None
    if ncx_id and ncx_id in manifest:
        ncx_item = manifest[ncx_id]
    if ncx_item is None:
        # Some EPUBs ship NCX without the toc attribute; sniff by media-type.
        for item in manifest.values():
            if item.media_type == "application/x-dtbncx+xml":
                ncx_item = item
                break
    if ncx_item is not None and reader.exists(ncx_item.href):
        try:
            return _parse_ncx(reader.read(ncx_item.href), ncx_item.href, spine)
        except Exception as exc:
            logger.warning("NCX parse failed (%s); returning empty TOC", exc)
    return ()


def _href_to_spine_index(
    href: str,
    base_path: str,
    spine: tuple[EpubSpineItem, ...],
) -> tuple[int, str | None]:
    """Resolve an NCX/nav href relative to the TOC file's directory.

    Returns ``(spine_index, anchor_id)``; ``spine_index`` is -1 if the
    href doesn't match any spine entry (common for sub-section anchors
    that all share one chapter).
    """
    anchor: str | None = None
    target = href
    if "#" in href:
        target, anchor = href.split("#", 1)
    if not target:
        # Pure anchor (#anchor) — applies to the same file as base_path,
        # but TOC entries should always identify a chapter file. Bail.
        return -1, anchor
    base_dir = posixpath.dirname(base_path)
    resolved = posixpath.normpath(posixpath.join(base_dir, target)) if base_dir else target
    resolved = resolved.lstrip("./")
    for index, item in enumerate(spine):
        if posixpath.normpath(item.href.lstrip("./")) == resolved:
            return index, anchor
    return -1, anchor


def _parse_nav(
    data: bytes,
    nav_path: str,
    spine: tuple[EpubSpineItem, ...],
) -> tuple[EpubTocItem, ...]:
    root = _parse_xml(data, nav_path)
    # The TOC nav is <nav epub:type="toc"> with an <ol> inside. We pick the
    # first matching nav (XHTML5 nav element) regardless of depth.
    nav_el: etree._Element | None = None
    for nav in root.iter(f"{{{_XHTML_NS}}}nav"):
        epub_type = nav.attrib.get(f"{{{_EPUB_NS}}}type", "")
        if "toc" in epub_type.split():
            nav_el = nav
            break
    if nav_el is None:
        # Fall back to the first nav element if no @epub:type=toc match.
        nav_el = next(iter(root.iter(f"{{{_XHTML_NS}}}nav")), None)
    if nav_el is None:
        return ()
    ol = nav_el.find(f"{{{_XHTML_NS}}}ol")
    if ol is None:
        return ()
    return _nav_collect_items(ol, nav_path, spine, depth=0)


def _nav_collect_items(
    ol_element: etree._Element,
    nav_path: str,
    spine: tuple[EpubSpineItem, ...],
    *,
    depth: int,
) -> tuple[EpubTocItem, ...]:
    items: list[EpubTocItem] = []
    for li in ol_element.findall(f"{{{_XHTML_NS}}}li"):
        anchor_el = li.find(f"{{{_XHTML_NS}}}a")
        if anchor_el is None:
            continue
        href = anchor_el.attrib.get("href")
        if not href:
            continue
        label = "".join(anchor_el.itertext()).strip() or href
        spine_index, anchor_id = _href_to_spine_index(href, nav_path, spine)
        children = ()
        nested_ol = li.find(f"{{{_XHTML_NS}}}ol")
        if nested_ol is not None:
            children = _nav_collect_items(nested_ol, nav_path, spine, depth=depth + 1)
        items.append(
            EpubTocItem(
                label=label,
                href=href,
                spine_index=spine_index,
                anchor_id=anchor_id,
                depth=depth,
                children=children,
            )
        )
    return tuple(items)


def _parse_ncx(
    data: bytes,
    ncx_path: str,
    spine: tuple[EpubSpineItem, ...],
) -> tuple[EpubTocItem, ...]:
    root = _parse_xml(data, ncx_path)
    nav_map = root.find(f"{{{_NCX_NS}}}navMap")
    if nav_map is None:
        return ()
    return _ncx_collect_items(nav_map, ncx_path, spine, depth=0)


def _ncx_collect_items(
    parent: etree._Element,
    ncx_path: str,
    spine: tuple[EpubSpineItem, ...],
    *,
    depth: int,
) -> tuple[EpubTocItem, ...]:
    items: list[EpubTocItem] = []
    for nav_point in parent.findall(f"{{{_NCX_NS}}}navPoint"):
        label_el = nav_point.find(f"{{{_NCX_NS}}}navLabel/{{{_NCX_NS}}}text")
        label = _text_or_none(label_el) or "(untitled)"
        content_el = nav_point.find(f"{{{_NCX_NS}}}content")
        href = content_el.attrib.get("src") if content_el is not None else None
        if not href:
            continue
        spine_index, anchor_id = _href_to_spine_index(href, ncx_path, spine)
        children = _ncx_collect_items(nav_point, ncx_path, spine, depth=depth + 1)
        items.append(
            EpubTocItem(
                label=label,
                href=href,
                spine_index=spine_index,
                anchor_id=anchor_id,
                depth=depth,
                children=children,
            )
        )
    return tuple(items)


def flatten_toc(items: tuple[EpubTocItem, ...]) -> tuple[EpubTocItem, ...]:
    """Depth-first flatten preserving order; useful for list-style UIs."""
    out: list[EpubTocItem] = []
    for item in items:
        out.append(item)
        if item.children:
            out.extend(flatten_toc(item.children))
    return tuple(out)


__all__ = [
    "EpubAssetReader",
    "EpubBook",
    "EpubError",
    "EpubManifestItem",
    "EpubMetadata",
    "EpubPasswordRequired",
    "EpubSpineItem",
    "EpubTocItem",
    "InvalidEpubError",
    "ZipFileAssetReader",
    "flatten_toc",
    "open_epub",
]
