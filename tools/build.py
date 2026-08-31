#!/usr/bin/env python3
"""Build the deterministic installable Kodi zip for EZ Maintenance++.

Same discipline as the fleet's other build pipelines (tony7bones.github.io's
`_tools/generate_repo.py:_zip_addon`): members
are collected in stable path-sorted order and written with fixed 1980-01-01
timestamps + 0644 perms, so the zip is byte-for-byte reproducible across
machines and runs (Kodi's version-based auto-upgrade breaks on same-version
byte churn, and a reproducible artifact is what makes a sha256 in a release's
notes actually mean something).

Output: dist/script.ezmaintenanceplusplus-<version>.zip (top-level folder in
the archive is the add-on id, exactly what Kodi's "Install from zip" expects).

Usage:
    python3 tools/build.py            # build once
    python3 tools/build.py --check    # build twice, byte-compare (determinism gate)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ADDON_ID = "script.ezmaintenanceplusplus"
ADDON_DIR = os.path.join(ROOT, ADDON_ID)
DIST_DIR = os.path.join(ROOT, "dist")

_CRUFT_DIRS = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
_CRUFT_SUFFIXES = (".pyc", ".pyo", ".DS_Store")


def read_version() -> str:
    with open(os.path.join(ADDON_DIR, "addon.xml"), encoding="utf-8") as f:
        xml = f.read()
    m = re.search(r'<addon\b[^>]*\bversion="([^"]+)"', xml)
    if not m:
        raise SystemExit("FATAL: could not read version from addon.xml")
    return m.group(1)


def _members() -> list[tuple[str, str]]:
    """(abs path, archive name) pairs, path-sorted, cruft excluded."""
    members = []
    for dirpath, dirs, files in os.walk(ADDON_DIR):
        dirs[:] = sorted(d for d in dirs if d not in _CRUFT_DIRS)
        for fname in sorted(files):
            if fname.endswith(_CRUFT_SUFFIXES):
                continue
            fpath = os.path.join(dirpath, fname)
            arcname = os.path.relpath(fpath, ROOT)  # keeps the "<id>/..." prefix
            members.append((fpath, arcname))
    members.sort(key=lambda m: m[1])
    return members


def build_zip(out_path: str) -> None:
    members = _members()
    if not members:
        raise SystemExit(f"FATAL: no files found under {ADDON_DIR}")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath, arcname in members:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(fpath, "rb") as fh:
                zf.writestr(info, fh.read())


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="build twice and byte-compare")
    args = ap.parse_args()

    version = read_version()
    os.makedirs(DIST_DIR, exist_ok=True)
    out = os.path.join(DIST_DIR, f"{ADDON_ID}-{version}.zip")

    if args.check:
        a = out + ".check-a"
        b = out + ".check-b"
        build_zip(a)
        build_zip(b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            same = fa.read() == fb.read()
        sha = sha256_file(a) if same else None
        os.remove(a)
        os.remove(b)
        if not same:
            print(
                "FATAL: determinism check FAILED - two builds differ", file=sys.stderr
            )
            return 1
        print(f"determinism check PASSED ({sha})")
        return 0

    build_zip(out)
    sha = sha256_file(out)
    print(f"built {os.path.relpath(out, ROOT)} (sha256 {sha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
