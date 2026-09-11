"""Language preferences, fallback, and live presentation without state loss."""
from __future__ import annotations

import ast
import json
from string import Formatter

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLabel, QLineEdit, QWidget

from joyread.infrastructure.config.settings_store import SettingsStore
from joyread.infrastructure.i18n import locale_service as locale
from joyread.infrastructure.resources.resource_loader import ResourceLoader
from joyread.ui.widgets.hidden_space_lock import HiddenSpaceLockOverlay
from joyread.ui.widgets.localized_text import LocalizedLabel, set_localized
from joyread.ui.widgets.dialogs import JoyReadDialogOverlay
from joyread.ui.widgets.reader_settings_panel import ReaderSettingsPanel
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel


@pytest.fixture(autouse=True)
def english_locale():
    locale.init(ResourceLoader().locale_dir(), None, "English")
    yield
    locale.load_language("English")


@pytest.mark.parametrize("tags,fallback,expected", [
    (["zh-Hant-TW"], "", "zh"), (["zh_CN.UTF-8"], "", "zh"),
    (["ja_JP"], "", "ja"), (["en-AU"], "", "en"),
    (["fr-FR", "ja-JP", "en"], "", "ja"),
    (["en", "zh"], "", "en"), (["de"], "zh_CN", "en"),
    ([], "ja_JP.UTF-8", "ja"), ([], "C", "en"),
    (["POSIX"], "", "en"), ([], "", "en"),
])
def test_system_language_preferences(tags, fallback, expected):
    assert locale.resolve_system_language(tags, fallback) == expected


def test_settings_compatibility_and_read_only_startup(tmp_path):
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "books")
    assert store.read_language_preference() == "System"
    assert not store.settings_path.exists()
    assert store.load().language == "System"
    for saved, expected in [("Chinese", "Chinese"), ("English", "English"), ("Japanese", "Japanese"), (None, "System")]:
        raw = json.loads(store.settings_path.read_text())
        if saved is None:
            raw.pop("language")
        else:
            raw["language"] = saved
        store.settings_path.write_text(json.dumps(raw))
        assert store.read_language_preference() == expected
        assert store.load().language == expected
    store.settings_path.write_text("{broken")
    assert store.read_language_preference() == "System"


def test_system_preference_remains_system_and_can_be_reselected(monkeypatch, tmp_path):
    from PySide6.QtCore import QLocale
    class SystemLocale:
        languages = ["zh-Hant"]
        def uiLanguages(self): return self.languages
        def name(self): return "zh_TW"
    system = SystemLocale()
    monkeypatch.setattr(QLocale, "system", lambda: system)
    store = SettingsStore(support_root=tmp_path, default_storage_root=tmp_path / "books")
    vm = SettingsViewModel(settings_store=store)
    vm.set_language("System")
    assert locale.active_language_code() == "zh"
    assert store.load().language == "System"
    vm.set_language("Japanese")
    assert locale.active_language_code() == "ja"
    system.languages = ["en"]
    vm.set_language("System")
    assert locale.active_language_code() == "en"
    system.languages = ["ja"]
    vm.set_language("System")
    assert locale.active_language_code() == "ja"
    assert vm.language == "System"
    locale.load_language("unsupported")
    assert locale.active_language_code() == "en"


def test_override_merge_empty_invalid_and_english_fallback(tmp_path):
    bundled, user = tmp_path / "bundled", tmp_path / "user"
    bundled.mkdir(); user.mkdir()
    (bundled / "en.json").write_text(json.dumps({"a":"English", "b":"Fallback"}))
    (bundled / "zh.json").write_text(json.dumps({"a":"中文", "c":"原文"}))
    (user / "zh.json").write_text(json.dumps({"a":"覆盖", "c":"", "d":5}))
    service = locale.LocaleService(bundled, user)
    service.load("Chinese")
    assert [service.t(k) for k in ("a", "b", "c", "missing")] == ["覆盖", "Fallback", "原文", "missing"]
    for bad in ("{broken", "[]"):
        (user / "zh.json").write_text(bad)
        service.load("Chinese")
        assert service.t("a") == "中文"


def test_catalogue_keys_and_parameters_match():
    tables = {}
    for code in ("en", "zh", "ja"):
        data = json.loads((ResourceLoader().locale_dir() / f"{code}.json").read_text())
        flat = {}; locale._flatten(data, "", flat)
        tables[code] = flat
    def fields(text):
        return {field for _, field, _, _ in Formatter().parse(text) if field is not None}
    for code in ("zh", "ja"):
        assert tables[code].keys() == tables["en"].keys()
        for key, text in tables["en"].items():
            assert fields(text) == fields(tables[code][key]), (code, key)


def test_live_widgets_keep_user_content_and_input(qtbot):
    parent = QWidget(); qtbot.addWidget(parent)
    translated = LocalizedLabel(locale.t("app.name"), parent)
    user_label = LocalizedLabel("JoyRead", parent)
    field = QLineEdit(parent)
    set_localized(field, "setPlaceholderText", locale.t("dialog.password_header"))
    field.setText("Secret123"); field.setSelection(1, 4)
    panel = ReaderSettingsPanel(ResourceLoader(), parent)
    for language, expected in [("Chinese", "欣阅"), ("Japanese", "ジョイヨミ")]:
        locale.load_language(language)
        assert translated.text() == expected
        assert user_label.text() == "JoyRead"
        assert field.text() == "Secret123" and field.selectedText() == "ecre"
        assert field.placeholderText() == locale.t("dialog.password_header")
        assert locale.t("reader.section_horizontal") in [label.text() for label in panel.findChildren(QLabel)]
    set_localized(translated, "setText", "User title")
    locale.load_language("English")
    assert translated.text() == "User title"
    translated.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    locale.load_language("Japanese")  # destroyed receivers automatically disconnect


def test_password_overlay_retranslates_without_dismissing(qtbot):
    root = QWidget(); qtbot.addWidget(root); root.resize(900, 700); root.show()
    lock = HiddenSpaceLockOverlay(root, hint="User hint", verify=lambda value: value == "Good123")
    lock._password.setText("Wrong123"); lock._on_verify_clicked()
    locale.load_language("Chinese")
    assert lock._state_label.text() == "密码错误。"
    assert lock._password.text() == "Wrong123"
    assert lock._hint_label.text() == "提示：User hint"
    verified, dismissed = [], []
    lock.verified.connect(lambda: verified.append(True))
    lock.dismissed.connect(lambda: dismissed.append(True))
    lock._password.setText("Good123"); lock._on_verify_clicked()
    lock._on_hide_clicked()
    assert verified == [True] and dismissed == [True]

    overlay = JoyReadDialogOverlay(root, ResourceLoader())
    accepted = []
    overlay.show_password_input(locale.t("dialog.unlock_title"), locale.t("dialog.password_header"), on_confirm=accepted.append)
    edit = overlay.findChild(QLineEdit); edit.setText("Keep123"); edit.setFocus()
    locale.load_language("Japanese"); qtbot.wait(10)
    assert overlay.isVisible() and not accepted
    assert edit.text() == "Keep123"
    assert overlay._panel.title_text == locale.t("dialog.unlock_title")


def test_qt_translation_resources_and_application_identity(qapp):
    original = qapp.applicationName()
    for language, name in [("Chinese", "欣阅"), ("Japanese", "ジョイヨミ"), ("English", "JoyRead")]:
        locale.load_language(language)
        assert qapp.applicationDisplayName() == name
        assert qapp.applicationName() == original
        assert (getattr(qapp, "_joyread_qt_translator", None) is not None) == (language != "English")


def test_user_visible_text_sinks_do_not_introduce_english_literals():
    """Guard display entry points, not arbitrary technical strings or logs."""
    root = ResourceLoader().package_root / "ui"
    allowed = {"!", "px"}  # symbols/units; no translated prose exemptions
    failures = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call): continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name not in {"QLabel", "LocalizedLabel", "QPushButton", "LocalizedPushButton", "set_localized", "setToolTip", "setPlaceholderText", "setWindowTitle"}: continue
            arguments = node.args[2:3] if name == "set_localized" else node.args[:1]
            for argument in arguments:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    value = argument.value
                    if any(c.isascii() and c.isalpha() for c in value) and value not in allowed:
                        failures.append((str(path.relative_to(root)), node.lineno, value))
    assert not failures, failures


def test_multiple_readers_keep_documents_and_pages_on_language_change(qtbot, tmp_path):
    import io
    import zipfile
    from PIL import Image
    from joyread.app.app_context import create_app_context
    from joyread.ui.views.main_window import MainWindow
    from joyread.ui.views.reader_window import ReaderWindow

    source = tmp_path / "Reader book.cbz"
    image = io.BytesIO(); Image.new("RGB", (80, 120), "white").save(image, "PNG")
    with zipfile.ZipFile(source, "w") as archive:
        for i in range(3): archive.writestr(f"{i}.png", image.getvalue())
    store = SettingsStore(support_root=tmp_path / "support", default_storage_root=tmp_path / "library")
    store.update(language="English")
    context = create_app_context(settings_store=store)
    library = MainWindow(context)
    readers = [ReaderWindow(context, source) for _ in range(2)]
    try:
        library.show()
        for reader in readers:
            reader.show()
        qtbot.waitUntil(lambda: all(r.viewmodel.page_count == 3 and not r.viewmodel.is_loading for r in readers), timeout=10000)
        readers[0].viewmodel.seek(1)
        readers[1].viewmodel.seek(2)
        documents = [r.viewmodel._document for r in readers]
        pages = [r.viewmodel.current_index for r in readers]
        for language, name in [("Chinese", "欣阅"), ("Japanese", "ジョイヨミ")]:
            context.settings_viewmodel.set_language(language)
            qtbot.wait(10)
            assert library.windowTitle() == name
            for index, reader in enumerate(readers):
                assert reader.viewmodel._document is documents[index]
                assert reader.viewmodel.current_index == pages[index]
                assert reader.windowTitle() == "Reader book"
                assert reader.shell.header.back_button.toolTip() == locale.t("reader.back")
    finally:
        for reader in readers: reader.close()
        library.close(); context.close()


def test_file_filter_translates_label_but_keeps_canonical_value(qtbot):
    from joyread.ui.widgets.top_toolbar import TopToolbarWidget
    toolbar = TopToolbarWidget(ResourceLoader())
    qtbot.addWidget(toolbar)
    values = []
    toolbar.filter_changed.connect(values.append)
    dropdown = toolbar._filter_dropdown
    for language, label in [("Chinese", "全部"), ("Japanese", "すべて")]:
        locale.load_language(language)
        assert dropdown.value == "ALL"
        assert dropdown._label.text() == label
        dropdown.set_value("PDF", emit=True)
        assert values[-1] == "PDF"
        dropdown.set_value("ALL", emit=True)
        assert values[-1] == "ALL"
        assert dropdown._label.text() == label


def test_hidden_space_service_failures_are_localized_at_viewmodel_boundary(tmp_path):
    from joyread.core.services.hidden_space_service import HiddenSpacePasswordError
    class RejectingService:
        def change_password(self, *args):
            raise HiddenSpacePasswordError("Current password is incorrect.", code="incorrect_current")
    vm = SettingsViewModel(hidden_space_service=RejectingService())
    errors = []
    vm.hidden_space_error.connect(errors.append)
    locale.load_language("Chinese")
    assert vm.change_hidden_space_password("wrong", "New123", "New123") is False
    assert errors == ["当前密码错误。"]
