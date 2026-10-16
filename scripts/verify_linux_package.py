#!/usr/bin/env python3
"""Check the installed Linux package's glibc baseline and isolated startup.

Run on Ubuntu 22.04 after installing the DEB. This checks the frozen executable,
not the build environment's Python imports; source tests cannot catch a bundled
Shiboken or system library that requires a newer glibc. Offscreen startup is not
a substitute for interactive X11/Wayland and document activation validation.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from zipfile import ZipFile

from PIL import Image


MAX_GLIBC = (2, 35)


def required_glibc_versions(output: str) -> set[tuple[int, ...]]:
    return {tuple(map(int, match.split('.'))) for match in re.findall(r'Name: GLIBC_(\d+(?:\.\d+)+)', output)}


def verify_glibc(app_dir: Path) -> None:
    failures = []
    checked = 0
    for path in app_dir.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        result = subprocess.run(['readelf', '--version-info', str(path)], check=True, capture_output=True, text=True)
        versions = required_glibc_versions(result.stdout)
        if versions and max(versions) > MAX_GLIBC:
            failures.append(f'{path.relative_to(app_dir)}: GLIBC_{".".join(map(str, max(versions)))}')
        checked += 1
    if not checked:
        raise SystemExit('No ELF binaries found in the installed package.')
    if failures:
        raise SystemExit('Libraries exceed Ubuntu 22.04 glibc 2.35:\n' + '\n'.join(failures))
    print(f'Checked {checked} ELF binaries: all require glibc <= 2.35.', flush=True)


def reader_frame_reached(console: str) -> bool:
    """Require an actual frame callback, not merely a ready Reader shell."""

    for line in console.splitlines():
        _, marker, payload = line.partition('reader_perf ')
        if not marker:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if event.get('event') == 'gui.pixmap' and event.get('action') == 'converted':
            return True
    return False


def verify_startup(app_dir: Path, *, webp_reader: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix='joyread-package-smoke-') as temporary:
        runtime = Path(temporary)
        env = dict(os.environ, JOYREAD_RUNTIME_DIR=str(runtime), QT_QPA_PLATFORM='offscreen')
        # Do not borrow native libraries or Python modules from the build env.
        for key in ('LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(key, None)
        command = [str(app_dir / 'JoyRead')]
        if webp_reader:
            # Exercise the frozen Pillow codec, not only the build Python's
            # imports: Qt 6.8 ARM64 cannot load its native WebP plugin here.
            encoded = BytesIO()
            Image.new('RGB', (80, 120), (40, 100, 180)).save(encoded, 'WEBP')
            document = runtime / 'image-codec-smoke.cbz'
            with ZipFile(document, 'w') as archive:
                archive.writestr('001.webp', encoded.getvalue())
            command.append(str(document))
            env['JOYREAD_READER_PERF'] = '1'
        with (runtime / 'console.log').open('w+') as console:
            process = subprocess.Popen(command, cwd=runtime, env=env, stdout=console, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    output = (runtime / 'console.log').read_text(errors='replace')
                    ready = reader_frame_reached(output) if webp_reader else 'JoyRead primary runtime is ready' in output
                    if ready:
                        result = 'prepared a WebP Reader frame for the GUI' if webp_reader else 'reached process.ready'
                        print(f'Installed executable {result} with an isolated library.', flush=True)
                        return
                    time.sleep(0.2)
                console.seek(0)
                raise SystemExit(f'Installed executable did not complete its startup check (webp_reader={webp_reader}, exit={process.poll()}):\n{console.read()}')
            finally:
                # This is a disposable launch check; no user library is opened.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_dir', type=Path)
    args = parser.parse_args()
    app_dir = args.app_dir.resolve()
    verify_glibc(app_dir)
    verify_startup(app_dir)
    verify_startup(app_dir, webp_reader=True)


if __name__ == '__main__':
    main()
