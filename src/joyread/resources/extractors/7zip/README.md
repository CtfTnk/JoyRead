Bundled 7-Zip extractors
========================

JoyRead resolves the 7-Zip command-line backend from here first, falling back to
`JOYREAD_7ZIP_PATH` and then `7zz`/`7z` on `PATH`. The backend does the 7z and
RAR/CBR reading, including encrypted archives.

Vendored: **7-Zip 26.02** (2026-06-25), from https://github.com/ip7z/7zip/releases

| Directory | Files | Taken from |
| --- | --- | --- |
| `darwin-arm64/` | `7zz` | `7z2602-mac.tar.xz` (universal x86_64 + arm64) |
| `linux-x86_64/` | `7zz` | `7z2602-linux-x64.tar.xz`, the `7zzs` build |
| `linux-arm64/` | `7zz` | `7z2602-linux-arm64.tar.xz`, the `7zzs` build |
| `windows-x86_64/` | `7z.exe`, `7z.dll` | `7z2602-x64.exe` installer |

Two things about that table are deliberate and easy to get wrong when updating.

**Linux takes `7zzs`, not `7zz`, and renames it.** The tarball ships both: `7zz`
is dynamically linked, `7zzs` is static. A dynamically linked helper links
against the build machine's glibc and fails on any distro older than it, which
is exactly the bug a bundled helper exists to avoid. It is renamed to `7zz`
because that is the name the resolver looks for on every platform.

**Windows takes `7z.exe` *and* `7z.dll`, not `7za.exe`.** There is no `7zz.exe`.
The only standalone Windows console build is `7za.exe`, which has "reduced
formats support" and cannot read RAR — so a `7za.exe` build would silently drop
CBR. `7z.exe` is a front end that opens nothing without `7z.dll` beside it, so
the two must be vendored and shipped together.

Not vendored: `darwin-x86_64` (the macOS binary is universal, so it only needs
copying into place if Intel Macs become a target) and `windows-arm64`.

Updating
--------

Fetch the release assets, then for each platform copy in the binary and the
`License.txt`, `readme.txt`, and `History.txt` beside it — `joyread.spec` ships
those three and refuses to build without the binary. Do not copy the `MANUAL/`
directory; nothing reads it and it is gitignored.
