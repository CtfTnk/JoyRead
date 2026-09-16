"""Native package versions derived from JoyRead's Python release version.

Loaded with runpy by build tools, without importing the application or the
unrelated PyPI package named ``packaging``.
"""
import re


def package_versions(version: str) -> dict[str, str]:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)([1-9]\d*))?", version)
    if match is None:
        raise ValueError(f"Unsupported release version: {version}")
    base, stage, number = match.groups()
    if number is not None and int(number) > 255:
        raise ValueError("macOS prerelease build number must be between 1 and 255")
    # CFBundleShortVersionString has three numeric components. CFBundleVersion
    # uses Apple's 'fc' suffix for a release candidate; Debian needs '~' so the
    # prerelease sorts below the same final version.
    suffix = f"{stage}{number}" if stage else ""
    mac_suffix = f"{'fc' if stage == 'rc' else stage}{number}" if stage else ""
    return {
        "display": version,
        "mac_short": base,
        "mac_build": base + mac_suffix,
        "debian_upstream": base + (f"~{suffix}" if suffix else ""),
    }
