"""The P0Ref desktop build must not touch the normal JoyRead profile."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


HOOK = Path(__file__).resolve().parents[2] / "scripts" / "p0_ref_runtime.py"


def test_p0_ref_uses_local_report_profile_in_checkout(monkeypatch, tmp_path: Path) -> None:
    checkout = tmp_path / "JoyRead"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname = 'joyread'\n")
    executable = checkout / "dist" / "JoyRead-P0Ref" / "JoyRead-P0Ref.exe"
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delenv("JOYREAD_RUNTIME_DIR", raising=False)

    runpy.run_path(str(HOOK))

    assert Path(os.environ["JOYREAD_RUNTIME_DIR"]) == checkout / "reports" / "p0-ref-profile"


def test_p0_ref_preserves_explicit_benchmark_profile(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "benchmark-run"
    monkeypatch.setenv("JOYREAD_RUNTIME_DIR", str(profile))

    runpy.run_path(str(HOOK))

    assert Path(os.environ["JOYREAD_RUNTIME_DIR"]) == profile


def test_p0_ref_uses_separate_local_app_data_when_moved(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "standalone" / "JoyRead-P0Ref" / "JoyRead-P0Ref.exe"
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("JOYREAD_RUNTIME_DIR", raising=False)

    runpy.run_path(str(HOOK))

    assert Path(os.environ["JOYREAD_RUNTIME_DIR"]) == local_app_data / "JoyRead-P0Ref"
