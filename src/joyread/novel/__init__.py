"""The novel (EPUB) reader, as a feature that can be present or absent.

Everything the novel reader needs lives under this package, and the rest of
the application never imports it: the composition root builds a
:class:`joyread.app.windows.novel_provider.NovelReaderProvider` from
:mod:`joyread.novel.app.provider` when ``EPUB_ACCESS_ENABLED`` is on, and
otherwise the app runs with no novel reader in it. Deleting this directory is
a supported operation, and ``tests/unit/test_epub_gate.py`` enforces the
import rule that keeps it that way.

Dependencies point one way only: modules here may import from the wider app
(shared chrome, theme, locale, task service), but nothing out there may import
from here. ``lxml`` is required by the EPUB parser and is packaged as the
``joyread[epub]`` extra rather than a base dependency.

This module is deliberately import-free -- no re-exports. Anything it pulled
in would load for every consumer of the package, which is the coupling this
package exists to remove.
"""
