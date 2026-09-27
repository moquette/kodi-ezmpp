"""Regression tests for the tvOS App-Sandbox cross-layer read quirk.

These EXECUTE the real shipped functions - dropbox_remote._qr_image and ui._open_reader -
against fake_kodi_sandbox_io (a tvOS-accurate model of Apple's scoped-resource behavior,
transcribed from the hardware-confirmed playbook). They cover the two bug classes the
storage fake deliberately does not:

  - ControlImage / texture write-through (the blank Dropbox QR, 2026.07.13.6)
  - VFS-cannot-read-a-foreign-local-file (the backup size mismatch, 2026.07.04.2-.5)

Each has a matching "reverted" test proving the fix is what makes it pass: swap the fix
back to the naive form and the tvOS read goes empty, exactly as it did on the box. That is
the house standard - a test that does not fail on the pre-fix code proves
nothing.
"""

import importlib
import os
import sys
import types

import pytest

ADDON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "script.ezmaintenanceplusplus",
)

sys.path.insert(0, os.path.dirname(__file__))
from fake_kodi_sandbox_io import FakeSandboxFS, make_modules  # noqa: E402


def _load(module_name, store, extra_stubs=None):
    """Import resources.lib.modules.<module_name> with tvOS fakes injected.

    Stubs the heavy top-level imports (xbmcgui, xbmcaddon, requests) that the module needs
    to import but the function under test never calls, and injects the sandbox-aware
    xbmc/xbmcvfs so the real function runs against the modeled tvOS behavior.
    """
    xbmc, xbmcvfs = make_modules(store)
    stubs = {
        "xbmc": xbmc,
        "xbmcvfs": xbmcvfs,
        "xbmcgui": types.ModuleType("xbmcgui"),
        "xbmcaddon": types.ModuleType("xbmcaddon"),
        "requests": types.ModuleType("requests"),
    }
    xbmcaddon = stubs["xbmcaddon"]
    xbmcaddon.Addon = lambda *a, **k: types.SimpleNamespace(
        getAddonInfo=lambda *a, **k: "",
        getSetting=lambda *a, **k: "",
        setSetting=lambda *a, **k: None,
    )
    stubs.update(extra_stubs or {})
    if ADDON not in sys.path:
        sys.path.insert(0, ADDON)
    saved = {k: sys.modules.get(k) for k in stubs}
    full = "resources.lib.modules.%s" % module_name
    saved[full] = sys.modules.get(full)
    try:
        for k, v in stubs.items():
            sys.modules[k] = v
        sys.modules.pop(full, None)
        return importlib.import_module(full), xbmcvfs
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# --------------------------------------------------------------------------- #
# Bug class: ControlImage / texture write-through (the blank Dropbox QR)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("platform", ["tvos", "android"])
def test_qr_image_is_readable_by_the_texture_loader(tmp_path, platform):
    """_qr_image's PNG must be readable back through xbmcvfs (what the texture loader uses).

    On tvOS this is true ONLY because _qr_image writes THROUGH xbmcvfs. If it regresses to
    plain open(), the reverted test below proves the texture goes blank.
    """
    store = FakeSandboxFS(tmp_path, platform=platform)
    mod, xbmcvfs = _load("dropbox_remote", store)

    special = mod._qr_image("https://example.test/auth?code=abc123")
    assert special.startswith("special://temp/") and special.endswith(".png")

    # The texture loader reads through the VFS. It must get real PNG bytes.
    data = xbmcvfs.File(special, "r").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "texture loader read a non-PNG / empty QR"
    assert len(data) > 100


def test_qr_written_with_plain_open_is_blank_on_tvos(tmp_path):
    """SELF-VERIFICATION: the reverted fix (plain open) => the VFS reads the QR EMPTY on tvOS.

    This is the exact failure that shipped a blank barcode on Apple TV. If this test does
    NOT fail-shaped (empty read) on the plain-open path, the fake no longer models the bug.
    """
    store = FakeSandboxFS(tmp_path, platform="tvos")
    # Simulate the pre-fix _qr_image: generate the PNG, write it with PLAIN open().
    import struct
    import zlib

    def _tiny_png():
        raw = b"\x00" + b"\xff\xff\xff" * 1  # 1x1 white-ish
        idat = zlib.compress(raw)

        def chunk(t, d):
            return (
                struct.pack(">I", len(d))
                + t
                + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b"")
        )

    special = "special://temp/_dbx_qr_plainopen.png"
    store.posix_write(special, _tiny_png())  # plain-open write - NOT through xbmcvfs

    _, xbmcvfs = make_modules(store)
    blank = xbmcvfs.File(special, "r").read()
    assert blank == b"", (
        "The fake no longer reproduces the blank-QR bug: a plain-open PNG must read EMPTY "
        "through the tvOS VFS. If this passes, the ControlImage regression test is toothless."
    )
    # ...and Stat still lies that the file is fine - the trap that hid it.
    assert xbmcvfs.Stat(special).st_size() > 0


# --------------------------------------------------------------------------- #
# Bug class: VFS cannot read a foreign local file (the backup size mismatch)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("platform", ["tvos", "android"])
def test_open_reader_reads_a_local_file_written_by_a_foreign_writer(tmp_path, platform):
    """ui._open_reader must return the real bytes of a local file written by plain open()
    (e.g. a zipfile-extracted backup). On tvOS that works ONLY because it uses plain open().
    """
    store = FakeSandboxFS(tmp_path, platform=platform)
    payload = b"backup-bytes-" + b"x" * 5000
    local = os.path.join(str(tmp_path), "temp", "backup.zip")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:  # foreign (plain) writer, like zipfile
        f.write(payload)
    store.vfs_written.discard(local)  # ensure it is NOT VFS-known

    mod, _ = _load("ui", store)
    reader = mod._open_reader(local)
    try:
        got = b""
        while True:
            chunk = reader.readBytes(4096)
            if not chunk:
                break
            got += chunk
    finally:
        reader.close()
    assert got == payload, "the backup read short/empty - the size-mismatch bug"


def test_reading_that_local_file_through_the_vfs_is_empty_on_tvos(tmp_path):
    """SELF-VERIFICATION: routing the local read through xbmcvfs (the regression) reads EMPTY
    on tvOS - the exact corruption that produced short/mismatched backups on Apple TV.
    """
    store = FakeSandboxFS(tmp_path, platform="tvos")
    local = os.path.join(str(tmp_path), "temp", "backup.zip")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(b"real-backup-payload")
    store.vfs_written.discard(local)

    _, xbmcvfs = make_modules(store)
    assert xbmcvfs.File(local, "r").readBytes() == b"", (
        "The fake no longer reproduces the foreign-local-read bug: a plain-open local file "
        "must read EMPTY through the tvOS VFS."
    )
    # Stat reports the true size even though the read is empty - why it took four releases.
    assert xbmcvfs.Stat(local).st_size() == len(b"real-backup-payload")


def test_android_has_no_cross_layer_quirk(tmp_path):
    """On android/desktop, a plain-open local file reads fine through the VFS too - so the
    fixes are a no-op there, and neither reverting them nor keeping them changes behavior.
    """
    store = FakeSandboxFS(tmp_path, platform="android")
    local = os.path.join(str(tmp_path), "temp", "f.bin")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(b"content")
    store.vfs_written.discard(local)
    _, xbmcvfs = make_modules(store)
    assert xbmcvfs.File(local, "r").readBytes() == b"content"


# --------------------------------------------------------------------------- #
# Guard the FAKE itself (QA found the suite did not actually exercise these)
# --------------------------------------------------------------------------- #


def test_vfs_readbytes_advances_across_chunks(tmp_path):
    """A copy loop over xbmcvfs.File.readBytes(n) must terminate and reassemble the file.

    This guards the FAKE, not the shipped code. An earlier version of _File.readBytes
    returned the first n bytes on every call (no cursor advance). That passed every other
    test here - the only read-loop test resolves to _LocalReader (real open) and never drives
    the fake's readBytes - while it would spin forever or duplicate bytes in a real copy. QA
    proved the hole by reintroducing the bug and watching all 7 tests still pass. This is the
    test that fails when the cursor stops advancing.
    """
    store = FakeSandboxFS(tmp_path, platform="tvos")
    payload = (
        bytes(range(256)) * 40 + b"tail"
    )  # 10244 bytes - NOT a multiple of the chunk
    _, xbmcvfs = make_modules(store)
    w = xbmcvfs.File(
        "special://temp/chunky.bin", "w"
    )  # write THROUGH the vfs -> vfs-known
    w.write(bytearray(payload))
    w.close()

    r = xbmcvfs.File("special://temp/chunky.bin", "r")
    got, guard = b"", 0
    # A correct cursor needs ceil(10244/1000) = 11 reads. Cap just above that so a
    # non-advancing cursor trips in milliseconds, not by building a 100MB string first.
    while True:
        guard += 1
        assert guard <= 50, (
            "readBytes never returned empty - the cursor is not advancing"
        )
        chunk = r.readBytes(1000)
        if not chunk:
            break
        got += chunk
    r.close()
    assert got == payload, (
        "chunked read did not reassemble the file (cursor mis-advance)"
    )


# --------------------------------------------------------------------------- #
# The OTHER two co-equal causes of the blank QR (docs call them out; QA flagged
# them as untested). The write-through fix alone is not sufficient.
# --------------------------------------------------------------------------- #


def test_qr_image_uses_a_fresh_filename_each_call(tmp_path):
    """Kodi caches textures by PATH. Reusing one name makes a first failed load stick as a
    blank even after the file is rewritten - that is why the blank QR survived until a restart
    on the old grayscale build. Every call must return a distinct path. (Co-equal cause; the
    write-through fix does not address it.)
    """
    store = FakeSandboxFS(tmp_path, platform="tvos")
    mod, _ = _load("dropbox_remote", store)
    paths = {mod._qr_image("https://example.test/%d" % i) for i in range(5)}
    assert len(paths) == 5, (
        "_qr_image reused a path - Kodi's texture cache will serve a stale/blank QR"
    )


def test_qr_png_is_32bit_rgba_so_kodi_21_will_draw_it(tmp_path):
    """Kodi 21.3 refuses to draw a grayscale PNG and caches the failure as a blank barcode.
    The QR must be 32-bit RGBA: IHDR bit-depth 8, colour-type 6. Magic-bytes + length (the
    only thing the main test checked) would pass a grayscale regression. (Co-equal cause.)
    """
    store = FakeSandboxFS(tmp_path, platform="tvos")
    mod, xbmcvfs = _load("dropbox_remote", store)
    data = xbmcvfs.File(mod._qr_image("https://example.test/rgba"), "r").read()
    assert data[12:16] == b"IHDR"
    bit_depth, colour_type = data[24], data[25]
    assert (bit_depth, colour_type) == (8, 6), (
        "QR PNG is bit-depth %d / colour-type %d, not 32-bit RGBA (8/6). Kodi 21.3 refuses "
        "grayscale and caches the failure as a blank barcode."
        % (bit_depth, colour_type)
    )
