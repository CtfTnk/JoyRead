import importlib.util
from pathlib import Path
import plistlib
import runpy
import shutil
import subprocess
import tomllib

import pytest
from joyread import __version__

ROOT = Path(__file__).resolve().parents[2]
versions = runpy.run_path(str(ROOT/'packaging/version_info.py'))['package_versions']


def test_runtime_and_project_version_agree():
    assert __version__ == tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']


@pytest.mark.parametrize('value,mac,debian', [
    ('1.1.0rc1','1.1.0fc1','1.1.0~rc1'),
    ('1.1.0b2','1.1.0b2','1.1.0~b2'),
    ('1.1.0a1','1.1.0a1','1.1.0~a1'),
    ('1.1.0','1.1.0','1.1.0'),
])
def test_prerelease_platform_versions(value,mac,debian):
    result = versions(value)
    assert result == {'display':value,'mac_short':'1.1.0','mac_build':mac,'debian_upstream':debian}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_debian_builder_uses_prerelease_ordering():
    builder = load_script('build_linux_deb')
    assert builder.debian_package_version('1.1.0rc1') == '1.1.0~rc1-1'
    assert builder.debian_package_version('1.1.0') == '1.1.0-1'


def test_dmg_uses_full_version_from_built_bundle(tmp_path, monkeypatch):
    builder = load_script('build_dmg')
    app = tmp_path/'JoyRead.app';(app/'Contents').mkdir(parents=True)
    info = app/'Contents/Info.plist'
    info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'1.1.0','JoyReadVersion':'1.1.0rc1'}))
    monkeypatch.setattr(builder,'APP',app)
    assert builder.app_version() == '1.1.0rc1'
    info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'1.0.2'}))
    assert builder.app_version() == '1.0.2'


@pytest.mark.skipif(shutil.which('dpkg') is None, reason='dpkg not installed on this host')
def test_debian_rc_upgrades_to_final():
    subprocess.run(['dpkg','--compare-versions','1.1.0~rc1-1','lt','1.1.0-1'],check=True)
