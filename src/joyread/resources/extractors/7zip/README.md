Bundled 7-Zip extractors
========================

JoyRead looks here first when resolving the 7-Zip command-line backend used for
encrypted RAR/CBR extraction:

- `darwin-arm64/7zz`
- `darwin-x86_64/7zz`
- `linux-arm64/7zz`
- `linux-x86_64/7zz`
- `windows-arm64/7zz.exe`
- `windows-x86_64/7zz.exe`

If a bundled binary is absent, JoyRead falls back to `JOYREAD_7ZIP_PATH`, then
`7zz` or `7z` on `PATH`.

