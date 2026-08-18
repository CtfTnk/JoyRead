"""QTextBrowser-backed EPUB chapter renderer.

Reads chapter HTML straight from the parsed EPUB and resolves
``<img src>`` and ``<link rel="stylesheet">`` references through the
session's :class:`EpubAssetReader` so the engine never spills bytes
to disk. Mirrors the public API the shell already wires to.
"""

from __future__ import annotations

import logging
import posixpath
import re

from PySide6.QtCore import QByteArray, QEvent, QPoint, QTimer, QUrl, Qt, Signal as QtSignal
from PySide6.QtGui import QFont, QImage, QMouseEvent, QResizeEvent, QTextDocument
from PySide6.QtWidgets import QTextBrowser, QWidget

from joyread.novel.core.epub import EpubAssetReader
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.resources.styles.theme import Theme


logger = logging.getLogger(__name__)


class NovelContentArea(QTextBrowser):
    """Scrollable EPUB chapter view.

    Signal contract mirrors the previous skeleton so the novel shell's
    wiring is unchanged:

    - ``scroll_changed(fraction: float)`` — 0..1, emitted from the
      vertical scrollbar.
    - ``mouse_moved(QPoint)`` / ``left_clicked()`` / ``right_clicked()``
      — forwarded from the viewport event filter, drives chrome reveal
      + panel-close behaviour in the shell.
    """

    scroll_changed = QtSignal(float)
    mouse_moved = QtSignal(QPoint)
    left_clicked = QtSignal()
    right_clicked = QtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NovelContentArea")
        # Hide the inner scrollbar — the footer slider is the canonical
        # progress UI for the reader chrome.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Let the shell's rounded background show through the corners.
        self.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; padding: 0; }"
            "QTextBrowser QWidget { background: transparent; }"
        )
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")
        self.setFrameShape(self.Shape.NoFrame)
        # Right-click is for waking the chrome, not the system text menu.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.viewport().setMouseTracking(True)
        # Disable QTextBrowser's built-in anchor navigation; the topic
        # panel + footer paddles own all chapter transitions for now.
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        # Forward viewport mouse events without consuming them.
        self.viewport().installEventFilter(self)

        self._suppress_scroll_signal = False
        self._asset_reader: EpubAssetReader | None = None
        self._base_dir: str = ""
        self._language: str | None = None
        self._custom_enabled: bool = False
        self._disable_css: bool = False
        self._font_size: int = Theme.novel_placeholder_font_size
        # Last payload + viewport-width at render time. We re-render on
        # resize so pre-scaled images (see ``loadResource``) fit the new
        # viewport instead of overflowing it.
        self._current_payload = None
        self._last_render_viewport_width = 0
        # Track every URL we've handed to QTextDocument's resource cache
        # so we can evict cached (pre-scaled) QImages before a re-render.
        # QTextDocument has no public ``clearResources`` in PySide6 — the
        # only way to drop a cached entry is to overwrite it with an
        # invalid variant via ``addResource``.
        self._loaded_resource_urls: list[tuple[int, str]] = []
        self._rerender_timer = QTimer(self)
        self._rerender_timer.setSingleShot(True)
        self._rerender_timer.setInterval(150)
        self._rerender_timer.timeout.connect(self._rerender_for_viewport)

        self.verticalScrollBar().valueChanged.connect(self._emit_scroll_changed)
        self._apply_stylesheet()
        self._set_placeholder()

    # --- Public API consumed by the shell -----------------------------------

    def set_chapter(self, payload) -> None:  # noqa: ANN001 — NovelChapterPayload
        """Render a chapter coming from the viewmodel."""
        self._current_payload = payload
        self._render_payload(payload, reset_scroll=True)

    def _render_payload(self, payload, *, reset_scroll: bool) -> None:  # noqa: ANN001
        self._asset_reader = payload.asset_reader
        self._base_dir = payload.base_dir or ""
        self._language = payload.language
        # Fixed-layout EPUBs (the Okaasan sample is one) wrap each
        # page image in ``<svg><image xlink:href="..."/></svg>``.
        # QTextBrowser can't render SVG, so rewrite those wrappers to
        # plain ``<img>`` first; the centering pass below then wraps
        # them in a ``<p align="center">`` block which Qt does respect.
        html = _unwrap_svg_images(payload.html)
        html = _wrap_images_for_centering(html)
        # Replace the QTextDocument entirely on each render rather than
        # calling ``setHtml`` on the existing doc. The existing doc
        # caches resolved block formats (margins, auto-centering) from
        # the previous layout pass — a setHtml on top would re-parse
        # the HTML but reuse those stale per-block formats, leaving
        # ``margin: auto`` resolved against the OLD viewport width.
        # A fresh document gives every block a fresh format cascade.
        previous_fraction = 0.0 if reset_scroll else self.scroll_percentage()
        fresh_doc = QTextDocument(self)
        fresh_doc.setDocumentMargin(self.document().documentMargin())
        base = (
            QUrl(f"epub:/{self._base_dir.rstrip('/')}/")
            if self._base_dir
            else QUrl("epub:/")
        )
        fresh_doc.setBaseUrl(base)
        self._loaded_resource_urls.clear()
        self._suppress_scroll_signal = True
        try:
            self.setDocument(fresh_doc)
            # Re-attach the scrollbar→signal connection; setDocument
            # leaves the QTextBrowser's own vertical scrollbar in place
            # but the new document drives layout from scratch.
            self._apply_stylesheet()
            self.setHtml(html)
            bar = self.verticalScrollBar()
            span = bar.maximum() - bar.minimum()
            if reset_scroll or span <= 0:
                bar.setValue(bar.minimum())
            else:
                bar.setValue(bar.minimum() + int(round(previous_fraction * span)))
        finally:
            self._suppress_scroll_signal = False
        self._last_render_viewport_width = self.viewport().width()
        self.scroll_changed.emit(self.scroll_percentage())

    def _rerender_for_viewport(self) -> None:
        """Re-render the current chapter so images re-scale to the new viewport."""
        if self._current_payload is None:
            return
        if self.viewport().width() == self._last_render_viewport_width:
            return
        # Preserve scroll position across the rerender so the user
        # doesn't get jumped back to the top of the chapter when they
        # resize the window mid-read.
        self._render_payload(self._current_payload, reset_scroll=False)

    def show_error(self, message: str) -> None:
        self._asset_reader = None
        self.setHtml(
            f"<div style='padding: 48px; text-align: center; "
            f"font-size: {Theme.novel_placeholder_font_size}pt;'>"
            f"{_escape_html(message)}</div>"
        )

    def is_scrollable(self) -> bool:
        return self.verticalScrollBar().maximum() > 0

    def scroll_percentage(self) -> float:
        bar = self.verticalScrollBar()
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return 0.0
        return (bar.value() - bar.minimum()) / span

    def set_scroll_percentage(self, fraction: float) -> None:
        bar = self.verticalScrollBar()
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return
        value = bar.minimum() + int(round(max(0.0, min(1.0, fraction)) * span))
        if bar.value() == value:
            return
        # Re-entrancy guard: setting the scroll percentage from the
        # slider must not loop back through ``scroll_changed`` into the
        # slider itself.
        self._suppress_scroll_signal = True
        try:
            bar.setValue(value)
        finally:
            self._suppress_scroll_signal = False

    def scroll_by_viewport(self, direction: int) -> None:
        bar = self.verticalScrollBar()
        step = max(1, self.viewport().height() - self._font_size * 2)
        bar.setValue(bar.value() + direction * step)

    def apply_enable_custom(self, enabled: bool) -> None:
        self._custom_enabled = enabled
        self._apply_stylesheet()
        logger.debug("NovelContentArea apply_enable_custom enabled=%s", enabled)

    def apply_font_size(self, size: int) -> None:
        self._font_size = max(8, min(72, int(size)))
        self._apply_stylesheet()
        logger.debug("NovelContentArea apply_font_size size=%s", self._font_size)

    def apply_disable_css(self, disabled: bool) -> None:
        if self._disable_css == bool(disabled):
            return
        self._disable_css = bool(disabled)
        logger.debug("NovelContentArea apply_disable_css disabled=%s", self._disable_css)
        # Re-render so loadResource is asked fresh and respects the
        # new flag (preserving the current scroll position).
        if self._current_payload is not None:
            self._render_payload(self._current_payload, reset_scroll=False)

    # --- QTextBrowser resource hook ----------------------------------------

    def loadResource(self, resource_type: int, name: QUrl):  # type: ignore[override]
        """Resolve images and CSS via the open EPUB's asset reader.

        Qt calls this with the URL it constructs from ``setBaseUrl`` +
        the document's ``<img src>`` / ``<link href>`` attributes. We
        strip the ``epub:/`` scheme and read the bytes through the
        archive-relative asset reader.
        """
        if self._asset_reader is None:
            return super().loadResource(resource_type, name)
        archive_path = _archive_path_from_url(name, self._base_dir)
        if archive_path is None or not self._asset_reader.exists(archive_path):
            return super().loadResource(resource_type, name)
        try:
            data = self._asset_reader.read(archive_path)
        except Exception:
            logger.warning("loadResource failed for %s", archive_path, exc_info=True)
            return super().loadResource(resource_type, name)
        # Image type is 2 (QTextDocument::ImageResource); StyleSheet
        # is 3. Use QImage for images so QTextDocument can cache them
        # directly. When ``_disable_css`` is on, serve an empty
        # stylesheet for type-3 requests — the EPUB's own CSS
        # (Konosuba's, e.g.) can inject auto-margin paragraph rules
        # that Qt resolves against the original viewport width and
        # never re-resolves on resize, causing horizontal overflow at
        # small viewports. The user toggles this in the Custom panel
        # when their book misbehaves; default is off so books render
        # with their intended typography.
        self._loaded_resource_urls.append((int(resource_type), name.toString()))
        if int(resource_type) == int(QTextDocument.ResourceType.StyleSheetResource):
            if self._disable_css:
                return QByteArray(b"")
        if int(resource_type) == int(QTextDocument.ResourceType.ImageResource):
            image = QImage.fromData(data)
            if image.isNull():
                return super().loadResource(resource_type, name)
            # QTextDocument ignores CSS ``max-width: 100%`` when laying
            # out images — it sizes the document to the QImage's natural
            # pixel width, which spills the viewport for full-resolution
            # covers (Konosuba's cover is 1201 px on a ~1164 px
            # viewport). Pre-scaling here keeps the document inside the
            # viewport and the rendered image visually centred.
            max_w, max_h = self._image_bounds_for_viewport()
            if image.width() > max_w or image.height() > max_h:
                image = image.scaled(
                    max_w,
                    max_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            return image
        return QByteArray(data)

    def _image_bounds_for_viewport(self) -> tuple[int, int]:
        """Pixel box an image must fit inside to avoid horizontal overflow.

        Subtracts QTextDocument's per-side ``documentMargin`` (default
        4 px, 8 px total) plus a small 8 px safety buffer for the
        ``<p>`` block's own block-format spacing. This is the exact
        space available between the viewport edges and the block where
        an image is laid out — undershooting by even a single pixel
        leaves the doc the same width as the viewport and centres
        cleanly.
        """
        margin = int(self.document().documentMargin())
        usable_w = max(1, self.viewport().width() - 2 * margin - 8)
        usable_h = max(1, self.viewport().height() - 2 * margin - 8)
        return usable_w, usable_h

    def resizeEvent(self, event: QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Re-render after the user stops dragging so pre-scaled images
        # match the new viewport. Without this, shrinking the window
        # leaves the cached QImage larger than the viewport and the
        # document overflows horizontally again.
        if self._current_payload is not None and self.viewport().width() != self._last_render_viewport_width:
            self._rerender_timer.start()

    # --- Event filter for shell wiring -------------------------------------

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API.
        if watched is self.viewport():
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                self.mouse_moved.emit(event.position().toPoint())
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.RightButton:
                    self.right_clicked.emit()
                elif event.button() == Qt.MouseButton.LeftButton:
                    self.left_clicked.emit()
        return super().eventFilter(watched, event)

    # --- Internals ----------------------------------------------------------

    def _emit_scroll_changed(self, _value: int) -> None:
        if self._suppress_scroll_signal:
            return
        self.scroll_changed.emit(self.scroll_percentage())

    def _apply_stylesheet(self) -> None:
        # Default stylesheet wins over EPUB's own CSS only when Enable
        # Custom is on. Off → only the image-fit + alignment rules
        # apply; EPUB CSS controls typography. This matches the manga
        # reader's gated custom semantics.
        # ``max-width: 100%`` makes images shrink to viewport width
        # (fit-screen for portrait covers). The HTML wrapper in
        # ``_wrap_images_for_centering`` provides the horizontal
        # centering Qt's CSS subset can't express.
        img_rules = "img { max-width: 100%; height: auto; }\n"
        if self._custom_enabled:
            css = (
                f"body, p, div, span {{ font-size: {self._font_size}pt; "
                f"line-height: 1.6; }}\n"
                + img_rules
            )
        else:
            css = img_rules
        self.document().setDefaultStyleSheet(css)

    def _set_placeholder(self) -> None:
        self.setHtml(
            "<div style='padding: 48px; text-align: center;"
            " font-style: italic; opacity: 0.7;'>"
            f"{_escape_html(t('reader.opening_epub'))}</div>"
        )


def _archive_path_from_url(url: QUrl, base_dir: str) -> str | None:
    """Map a Qt document URL back to an archive-relative path.

    QTextDocument hands us either a relative URL (which Qt resolves
    against the document's baseUrl, giving us our ``epub:/`` scheme),
    an absolute URL with a fragment, or a path-only string.
    """
    if url is None or url.isEmpty():
        return None
    if url.scheme() == "epub":
        path = url.path()
        # The path Qt builds from setBaseUrl(epub:/OEBPS/) + src="img.jpg"
        # looks like ``/OEBPS/img.jpg``. Trim the leading slash.
        return path.lstrip("/")
    # Some EPUB docs use absolute paths; treat them as archive-relative.
    raw = url.toString()
    if raw.startswith("file://"):
        return None  # let Qt's default handler try
    if raw.startswith("/"):
        return raw.lstrip("/")
    if base_dir:
        joined = posixpath.normpath(posixpath.join(base_dir, raw))
        return joined.lstrip("./")
    return raw


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# QTextDocument ignores ``margin: auto`` on block elements, so the
# only reliable way to centre an image is to put it inside a
# ``<p align="center">``. The inline ``margin: 0; padding: 0`` neutralises
# any horizontal margins the EPUB's own stylesheet would otherwise
# cascade onto our injected ``<p>`` (Konosuba's ``.cover_image``
# wrapper resolves ``margin: 0em auto`` to ~37 px on each side, which
# pushed the document past the viewport at small widths).
_IMG_TAG_PATTERN = re.compile(r"<img\b[^>]*/?>", re.IGNORECASE)


def _wrap_images_for_centering(html: str) -> str:
    # A single-cell ``<table>`` with ``align="center"`` is the only
    # block Qt's HTML parser doesn't fold into a ``<p>`` (where the
    # EPUB stylesheet's paragraph cascade injects stale auto-margins
    # that shift the image right at small viewports). The table cell
    # owns its own layout, so the image lands dead-centre regardless
    # of which paragraph rules the EPUB ships.
    return _IMG_TAG_PATTERN.sub(
        lambda match: (
            '<table width="100%" border="0" cellpadding="0" cellspacing="0">'
            f'<tr><td align="center">{match.group(0)}</td></tr></table>'
        ),
        html,
    )


# Fixed-layout EPUBs wrap each page image inside an SVG ``<image>``
# element with ``xlink:href``. QTextBrowser has no SVG renderer, so
# rewrite to a plain ``<img>`` whose ``src`` Qt will fetch through
# our ``loadResource`` hook just like any other image.
_SVG_IMAGE_PATTERN = re.compile(
    r"<svg\b[^>]*>\s*<image\b[^>]*?(?:xlink:)?href\s*=\s*[\"']([^\"']+)[\"'][^>]*/?>\s*</svg>",
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_svg_images(html: str) -> str:
    return _SVG_IMAGE_PATTERN.sub(
        lambda match: f'<img src="{match.group(1)}"/>',
        html,
    )


__all__ = ["NovelContentArea"]
