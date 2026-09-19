# Bundled UI fonts

JoyRead loads Noto Sans SC and Noto Sans JP in Regular (400), Medium (500),
and Bold (700), listed in `Theme.font_files`. Medium is required by the UI's
500-weight styles; omitting it lets Qt silently substitute Regular.

The fonts identify as version 2.004. The two Medium files were downloaded
unmodified from the official Noto CJK `Sans2.004` release:

- [NotoSansSC-Medium.otf](https://github.com/notofonts/noto-cjk/blob/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Medium.otf)
  SHA-256: `7633f5a016d4dd95e685a69633d818aabc4644c4b08e26bd35b1b30c45ed5dda`
- [NotoSansJP-Medium.otf](https://github.com/notofonts/noto-cjk/blob/Sans2.004/Sans/SubsetOTF/JP/NotoSansJP-Medium.otf)
  SHA-256: `f396a3b57256e4515be9cb41f7aac54766d654890082a9f1b5c2451b5c093d8a`

License: [SIL Open Font License 1.1](OFL.txt).

Fonts are application resources; users do not need to install them system-wide.
The package-data font glob and PyInstaller UI resource directory include them.
