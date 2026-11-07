"""Hidden Space dialog flows: setup, the upgrade, unlock, recovery and upkeep.

Each flow is a short chain of dialogs on the window's dialog overlay. A slow
step (scrypt, or waiting on the sealing worker) runs on the SettingsViewModel's
task service while the overlay shows a progress panel; its result arrives on
the UI thread and opens the next dialog -- or the same one again with the
error in it, so a typo never costs the user what they already typed elsewhere.

Secrets stay inside one flow's closures: a passphrase until its task runs, and
a ``VaultDraft`` (the private key of a vault being set up) only until its
recovery code is confirmed or the flow is abandoned. Nothing here logs them.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog, QLineEdit, QWidget

from joyread.core.services.hidden_space_service import HiddenSpaceState
from joyread.core.services.vault_service import RecoveryCodeDraft, VaultDraft
from joyread.core.vault import RecoveryCode
from joyread.infrastructure.i18n.locale_service import t
from joyread.ui.viewmodels.settings_viewmodel import SettingsViewModel
from joyread.ui.widgets.dialogs import DialogRecoveryCodeContent, JoyReadDialogOverlay

try:  # pragma: no cover - always present with PySide6.
    from shiboken6 import isValid as _qt_object_alive
except ImportError:  # pragma: no cover
    def _qt_object_alive(_obj: object) -> bool:
        return True


logger = logging.getLogger(__name__)

_PASSWORD = QLineEdit.EchoMode.Password


def _pair(values: tuple[str, ...]) -> tuple[str, str]:
    first, second = (tuple(values) + ("", ""))[:2]
    return first, second


class HiddenSpaceFlows(QObject):
    """Run the Hidden Space dialogs for one window.

    ``on_cancelled`` runs whenever a flow that started from the Show
    Collections switch ends without showing Hidden Space, so the switch can
    snap back to what is true.
    """

    def __init__(
        self,
        dialogs: JoyReadDialogOverlay,
        viewmodel: SettingsViewModel,
        host: QWidget,
        *,
        on_cancelled: Callable[[], None],
    ) -> None:
        super().__init__(host)
        self._dialogs = dialogs
        self._vm = viewmodel
        self._host = host
        self._on_cancelled = on_cancelled

    # -- entry points ---------------------------------------------------------------

    def request_show(self) -> None:
        """Show Collections was switched on: set up, upgrade or unlock first."""

        state = self._vm.hidden_space_state
        if state is HiddenSpaceState.NONE:
            self.start_setup()
        elif state is HiddenSpaceState.LEGACY:
            self.start_upgrade()
        elif state is HiddenSpaceState.LOCKED:
            self.start_unlock()
        else:
            self._vm.set_show_hidden_collection(True)

    # -- setup ------------------------------------------------------------------------

    def start_setup(self, *, state_prompt: str | None = None) -> None:
        title = t("hidden_space.setup_title")

        def accepted(values: tuple[str, ...]) -> None:
            passphrase, confirm = _pair(values)
            self._confirm_if_short(
                passphrase,
                retry=self.start_setup,
                proceed=lambda: self._prepare_setup(passphrase, confirm),
            )

        self._dialogs.show_multi_password_input(
            title,
            (t("hidden_space.passphrase_header"), t("hidden_space.confirm_passphrase_header")),
            on_confirm=accepted,
            echo_modes=(_PASSWORD, _PASSWORD),
            confirm_text=t("hidden_space.btn_next"),
            validator=lambda values: self._vm.passphrase_rule_error(*_pair(values)),
            on_cancel=self._on_cancelled,
            detail_text=t("hidden_space.setup_detail"),
            state_prompt=state_prompt,
        )

    def _prepare_setup(self, passphrase: str, confirm: str) -> None:
        title = t("hidden_space.setup_title")
        self._dialogs.show_progress(title, t("hidden_space.preparing"))
        self._vm.prepare_hidden_space_setup(
            passphrase,
            confirm,
            on_success=self._guarded(lambda draft: self._present_new_vault(draft, title)),
            on_failure=self._guarded(lambda text: self.start_setup(state_prompt=text)),
        )

    # -- the upgrade from the earlier password ------------------------------------------

    def start_upgrade(self, *, state_prompt: str | None = None) -> None:
        title = t("hidden_space.upgrade_title")

        def accepted(password: str) -> None:
            self._dialogs.show_progress(title, t("hidden_space.checking"))
            self._vm.prepare_hidden_space_upgrade(
                password,
                on_success=self._guarded(self.continue_upgrade),
                on_failure=self._guarded(lambda text: self.start_upgrade(state_prompt=text)),
            )

        self._dialogs.show_password_input(
            title,
            t("hidden_space.password_header"),
            on_confirm=accepted,
            echo_mode=_PASSWORD,
            confirm_text=t("hidden_space.btn_unlock"),
            validator=self._required,
            on_cancel=self._on_cancelled,
            detail_text=t("hidden_space.upgrade_detail"),
            state_prompt=state_prompt,
        )

    def continue_upgrade(self, draft: VaultDraft) -> None:
        """The earlier password checked out: save the recovery code and finish."""

        self._present_new_vault(draft, t("hidden_space.upgrade_title"))

    def _present_new_vault(self, draft: VaultDraft, title: str) -> None:
        self._present_code(
            draft.recovery_code,
            title=title,
            message=t("hidden_space.code_msg"),
            on_confirmed=lambda typed: self._finish_setup(draft, typed, title),
            on_cancel=self._on_cancelled,
        )

    def _finish_setup(self, draft: VaultDraft, typed_code: str, title: str) -> None:
        self._dialogs.show_progress(title, t("hidden_space.finishing"))

        def finished(queued: int) -> None:
            message = t("hidden_space.ready_encrypting_msg") if queued else t("hidden_space.ready_msg")
            self._dialogs.show_info(title, message)

        def failed(text: str) -> None:
            self._dialogs.show_info(title, text)
            self._on_cancelled()

        self._vm.finish_hidden_space_setup(
            draft, typed_code, on_success=self._guarded(finished), on_failure=self._guarded(failed)
        )

    # -- unlocking ------------------------------------------------------------------------

    def start_unlock(
        self,
        *,
        then: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        title: str | None = None,
        detail_text: str | None = None,
        confirm_text: str | None = None,
        state_prompt: str | None = None,
    ) -> None:
        """Ask for the passphrase and unlock; ``then`` runs once unlocked."""

        cancel = on_cancel if on_cancel is not None else self._on_cancelled
        dialog_title = title or t("dialog.unlock_title")

        def again(text: str) -> None:
            self.start_unlock(
                then=then, on_cancel=on_cancel, title=title, detail_text=detail_text,
                confirm_text=confirm_text, state_prompt=text,
            )

        def unlocked(_result: object) -> None:
            self._dialogs.close_progress()
            if then is not None:
                then()

        def accepted(passphrase: str) -> None:
            self._dialogs.show_progress(dialog_title, t("hidden_space.unlocking"))
            self._vm.unlock_hidden_space(
                passphrase, on_success=self._guarded(unlocked), on_failure=self._guarded(again)
            )

        self._dialogs.show_password_input(
            dialog_title,
            t("hidden_space.passphrase_header"),
            on_confirm=accepted,
            echo_mode=_PASSWORD,
            confirm_text=confirm_text or t("hidden_space.btn_unlock"),
            validator=self._required,
            on_cancel=cancel,
            on_skip=lambda: self.start_recovery(then=then, on_cancel=on_cancel),
            skip_text=t("hidden_space.btn_recovery_code"),
            detail_text=detail_text,
            state_prompt=state_prompt,
        )

    def start_recovery(
        self,
        *,
        then: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        state_prompt: str | None = None,
    ) -> None:
        """Unlock with the recovery code; a new passphrase is required."""

        cancel = on_cancel if on_cancel is not None else self._on_cancelled
        title = t("hidden_space.recover_title")

        def code_entered(typed_code: str) -> None:
            self._choose_new_passphrase(
                title,
                detail_text=t("hidden_space.recover_new_passphrase_detail"),
                on_chosen=lambda passphrase, confirm: recover(typed_code, passphrase, confirm),
                on_cancel=cancel,
            )

        def recover(typed_code: str, passphrase: str, confirm: str) -> None:
            self._dialogs.show_progress(title, t("hidden_space.unlocking"))

            def recovered(_result: object) -> None:
                self._dialogs.show_info(title, t("hidden_space.recovered_msg"))
                if then is not None:
                    then()

            self._vm.recover_hidden_space(
                typed_code,
                passphrase,
                confirm,
                on_success=self._guarded(recovered),
                on_failure=self._guarded(
                    lambda text: self.start_recovery(then=then, on_cancel=on_cancel, state_prompt=text)
                ),
            )

        self._dialogs.show_input(
            title,
            t("hidden_space.recovery_code_header"),
            on_confirm=code_entered,
            validator=self._vm.recovery_code_format_error,
            on_cancel=cancel,
            detail_text=t("hidden_space.recover_detail"),
            state_prompt=state_prompt,
        )

    # -- upkeep: passphrase, recovery code ---------------------------------------------------

    def start_change_passphrase(self, *, state_prompt: str | None = None) -> None:
        title = t("hidden_space.change_passphrase_title")

        def validator(values: tuple[str, ...]) -> str | None:
            current, new, confirm = (tuple(values) + ("", "", ""))[:3]
            return self._required(current) or self._vm.passphrase_rule_error(new, confirm)

        def change(current: str, new: str, confirm: str) -> None:
            self._dialogs.show_progress(title, t("hidden_space.changing_passphrase"))
            self._vm.change_hidden_space_passphrase(
                current,
                new,
                confirm,
                on_success=self._guarded(lambda _result: self._dialogs.show_info(title, t("hidden_space.passphrase_changed"))),
                on_failure=self._guarded(lambda text: self.start_change_passphrase(state_prompt=text)),
            )

        def accepted(values: tuple[str, ...]) -> None:
            current, new, confirm = (tuple(values) + ("", "", ""))[:3]
            self._confirm_if_short(
                new,
                retry=self.start_change_passphrase,
                proceed=lambda: change(current, new, confirm),
            )

        self._dialogs.show_multi_password_input(
            title,
            (
                t("hidden_space.current_passphrase_header"),
                t("hidden_space.new_passphrase_header"),
                t("hidden_space.confirm_new_passphrase_header"),
            ),
            on_confirm=accepted,
            echo_modes=(_PASSWORD, _PASSWORD, _PASSWORD),
            confirm_text=t("dialog.btn_update"),
            validator=validator,
            state_prompt=state_prompt,
        )

    def start_new_recovery_code(self, *, state_prompt: str | None = None) -> None:
        title = t("hidden_space.new_code_title")

        def replace(draft: RecoveryCodeDraft, typed_code: str) -> None:
            self._dialogs.show_progress(title, t("hidden_space.finishing"))
            self._vm.replace_hidden_space_recovery_code(
                draft,
                typed_code,
                on_success=self._guarded(lambda _result: self._dialogs.show_info(title, t("hidden_space.new_code_saved"))),
                on_failure=self._guarded(lambda text: self._dialogs.show_info(title, text)),
            )

        def prepared(draft: RecoveryCodeDraft) -> None:
            self._present_code(
                draft.recovery_code,
                title=title,
                message=t("hidden_space.new_code_msg"),
                on_confirmed=lambda typed: replace(draft, typed),
                on_cancel=None,
            )

        def accepted(passphrase: str) -> None:
            self._dialogs.show_progress(title, t("hidden_space.checking"))
            self._vm.prepare_hidden_space_recovery_code(
                passphrase,
                on_success=self._guarded(prepared),
                on_failure=self._guarded(lambda text: self.start_new_recovery_code(state_prompt=text)),
            )

        self._dialogs.show_password_input(
            title,
            t("hidden_space.passphrase_header"),
            on_confirm=accepted,
            echo_mode=_PASSWORD,
            confirm_text=t("hidden_space.btn_next"),
            validator=self._required,
            detail_text=t("hidden_space.new_code_detail"),
            state_prompt=state_prompt,
        )

    # -- leaving: revert, erase --------------------------------------------------------------

    def start_revert(self) -> None:
        """Decrypt and unhide everything. The passphrase is the confirmation."""

        title = t("dialog.revert_hidden_title")
        if self._vm.hidden_space_state is HiddenSpaceState.LEGACY:
            self._dialogs.show_password_input(
                title,
                t("hidden_space.password_header"),
                on_confirm=lambda password: self._revert(legacy_password=password),
                echo_mode=_PASSWORD,
                confirm_text=t("dialog.btn_revert"),
                validator=self._required,
                detail_text=t("hidden_space.revert_legacy_detail"),
            )
            return
        # Decrypting needs the key, and asking for the passphrase is how the
        # old password-gated Revert confirmed itself: one step does both.
        self.start_unlock(
            then=self._revert,
            on_cancel=lambda: None,
            title=title,
            detail_text=t("hidden_space.revert_detail"),
            confirm_text=t("dialog.btn_revert"),
        )

    def _revert(self, legacy_password: str | None = None) -> None:
        title = t("dialog.revert_hidden_title")
        self._dialogs.show_progress(title, t("hidden_space.reverting"))

        def reverted(pending: int) -> None:
            message = t("hidden_space.reverted_pending_msg") if pending else t("dialog.hidden_reverted_msg")
            self._dialogs.show_info(title, message)

        self._vm.revert_hidden_space(
            on_success=self._guarded(reverted),
            on_failure=self._guarded(lambda text: self._dialogs.show_info(title, text)),
            legacy_password=legacy_password,
        )

    def start_reset(self) -> None:
        """Delete every hidden book and the vault. Needs no passphrase, on purpose:
        it is the way out for someone who lost both it and the recovery code."""

        title = t("dialog.reset_hidden_title")

        def erase() -> None:
            self._dialogs.show_progress(title, t("hidden_space.erasing"))
            self._vm.reset_hidden_space(
                on_success=self._guarded(lambda _result: self._dialogs.show_info(title, t("hidden_space.erased_msg"))),
                on_failure=self._guarded(lambda text: self._dialogs.show_info(title, text)),
            )

        self._dialogs.show_confirm(
            title,
            t("dialog.reset_hidden_msg"),
            on_confirm=erase,
            confirm_text=t("dialog.btn_erase"),
            cancel_text=t("dialog.btn_cancel"),
            destructive=True,
        )

    # -- shared steps -------------------------------------------------------------------------

    def _choose_new_passphrase(
        self,
        title: str,
        *,
        on_chosen: Callable[[str, str], None],
        on_cancel: Callable[[], None] | None,
        detail_text: str | None = None,
        state_prompt: str | None = None,
    ) -> None:
        def again() -> None:
            self._choose_new_passphrase(title, on_chosen=on_chosen, on_cancel=on_cancel, detail_text=detail_text)

        def accepted(values: tuple[str, ...]) -> None:
            passphrase, confirm = _pair(values)
            self._confirm_if_short(passphrase, retry=again, proceed=lambda: on_chosen(passphrase, confirm))

        self._dialogs.show_multi_password_input(
            title,
            (t("hidden_space.new_passphrase_header"), t("hidden_space.confirm_new_passphrase_header")),
            on_confirm=accepted,
            echo_modes=(_PASSWORD, _PASSWORD),
            confirm_text=t("hidden_space.btn_next"),
            validator=lambda values: self._vm.passphrase_rule_error(*_pair(values)),
            on_cancel=on_cancel,
            detail_text=detail_text,
            state_prompt=state_prompt,
        )

    def _confirm_if_short(
        self, passphrase: str, *, retry: Callable[[], None], proceed: Callable[[], None]
    ) -> None:
        """Under twelve characters, say plainly what a short passphrase protects."""

        if not self._vm.passphrase_is_short(passphrase):
            proceed()
            return
        self._dialogs.show_confirm(
            t("hidden_space.short_title"),
            t("hidden_space.short_msg"),
            on_confirm=proceed,
            on_cancel=retry,
            confirm_text=t("hidden_space.btn_use_anyway"),
            cancel_text=t("hidden_space.btn_choose_another"),
        )

    def _present_code(
        self,
        code: RecoveryCode,
        *,
        title: str,
        message: str,
        on_confirmed: Callable[[str], None],
        on_cancel: Callable[[], None] | None,
    ) -> None:
        """Show a recovery code, then have it typed back before it counts."""

        formatted = code.formatted()

        def copy(content: DialogRecoveryCodeContent) -> None:
            QGuiApplication.clipboard().setText(formatted)
            content.set_state_prompt(t("hidden_space.code_copied"))

        def save(content: DialogRecoveryCodeContent) -> None:
            path, _filter = QFileDialog.getSaveFileName(
                self._host,
                t("hidden_space.save_code_title"),
                str(Path.home() / t("hidden_space.code_file_name")),
                t("hidden_space.code_file_filter"),
            )
            if not path:
                return
            error = self._vm.export_recovery_code(code, Path(path))
            content.set_state_prompt(error or t("hidden_space.code_saved"))

        def show_code() -> None:
            self._dialogs.show_recovery_code(
                title,
                message,
                formatted,
                on_continue=confirm,
                on_copy=copy,
                on_save=save,
                on_cancel=on_cancel,
            )

        def confirm() -> None:
            self._dialogs.show_password_input(
                title,
                t("hidden_space.recovery_code_header"),
                on_confirm=on_confirmed,
                validator=lambda typed: self._vm.recovery_code_confirmation_error(code, typed),
                on_cancel=on_cancel,
                on_skip=show_code,
                skip_text=t("hidden_space.btn_show_code_again"),
                detail_text=t("hidden_space.confirm_code_detail"),
            )

        show_code()

    @staticmethod
    def _required(value: str) -> str | None:
        return None if value else t("hidden_space_error.passphrase_required")

    def _guarded(self, callback: Callable[..., None]) -> Callable[..., None]:
        """Drop a task result that arrives after this window has gone."""

        def run(*args: object) -> None:
            if not _qt_object_alive(self._dialogs):
                logger.debug("Hidden Space result dropped: its window is gone")
                return
            callback(*args)

        return run


__all__ = ["HiddenSpaceFlows"]
