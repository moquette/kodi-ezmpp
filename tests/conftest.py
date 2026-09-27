"""
Adversarial QA harness for EZ Maintenance++ Dropbox backup feature.

We import the REAL add-on module `dropbox_remote.py` under fully-faked Kodi
modules + a fake `requests`, exactly the way Kodi exposes them at runtime, so we
exercise the shipped REST/chunk/token/pagination logic directly. No source is
modified; everything here is test-only scaffolding.

The same fakes also back the `ui_mod` fixture, which imports the real `ui.py`
(the uniform dialog/progress/copy library) so its Progress gauge, cancel model,
and atomic chunked copy are exercised off-device.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).resolve().parent.parent / "script.ezmaintenanceplusplus"

# Every test file must behave the same run alone as in the full suite. The full
# run only passed because test_addon_id_seam.py (first alphabetically) leaves
# the REAL resources package and its addon_id seam in sys.modules; run on its
# own, a dozen files errored on "cannot import name 'addon_id'" or "No module
# named 'resources'". Import it here, once, for every file. addon_id imports no
# Kodi module at load, so this pulls in nothing else.
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
importlib.import_module("resources.lib.modules.addon_id")


# --------------------------------------------------------------------------- #
# Fake Kodi modules
# --------------------------------------------------------------------------- #
class _FakeAddon:
    """One shared settings dict across all Addon() instances (Kodi semantics)."""

    _settings = {}

    def __init__(self, id=None):
        self.id = id

    def getSetting(self, key):
        return _FakeAddon._settings.get(key, "")

    def setSetting(self, key, value):
        _FakeAddon._settings[key] = value

    def getAddonInfo(self, key):
        return {"id": "script.ezmaintenanceplusplus", "name": "EZ Maintenance++"}.get(
            key, ""
        )

    def openSettings(self):
        return None


class _FakeDialog:
    """Records every call so tests can assert heading / message / labels, while keeping
    the simple scripted return values the Dropbox tests already rely on."""

    last_ok = None
    inputs = []  # queue of strings to return from input()
    ok_calls = []
    yesno_calls = []
    notification_calls = []
    select_calls = []
    input_calls = []
    yesno_result = True
    select_result = -1

    @classmethod
    def reset(cls):
        cls.last_ok = None
        cls.inputs = []
        cls.ok_calls = []
        cls.yesno_calls = []
        cls.notification_calls = []
        cls.select_calls = []
        cls.input_calls = []
        cls.yesno_result = True
        cls.select_result = -1

    def ok(self, *a, **k):
        _FakeDialog.last_ok = a
        _FakeDialog.ok_calls.append((a, k))
        return True

    def input(self, *a, **k):
        _FakeDialog.input_calls.append((a, k))
        if _FakeDialog.inputs:
            return _FakeDialog.inputs.pop(0)
        return ""

    def select(self, *a, **k):
        _FakeDialog.select_calls.append((a, k))
        return _FakeDialog.select_result

    def notification(self, *a, **k):
        _FakeDialog.notification_calls.append((a, k))
        return None

    def yesno(self, *a, **k):
        _FakeDialog.yesno_calls.append((a, k))
        return _FakeDialog.yesno_result


class _FakeDialogProgress:
    """Records create/update/close and can be scripted to report a cancel after N
    iscanceled() polls (cancel_after)."""

    create_calls = []
    update_calls = []
    close_calls = 0
    cancel_after = None  # iscanceled() returns True on/after this poll count (1-based)
    _polls = 0

    @classmethod
    def reset(cls):
        cls.create_calls = []
        cls.update_calls = []
        cls.close_calls = 0
        cls.cancel_after = None
        cls._polls = 0

    def create(self, *a, **k):
        _FakeDialogProgress.create_calls.append((a, k))
        return None

    def update(self, *a, **k):
        _FakeDialogProgress.update_calls.append((a, k))
        return None

    def close(self, *a, **k):
        _FakeDialogProgress.close_calls += 1
        return None

    def iscanceled(self):
        _FakeDialogProgress._polls += 1
        ca = _FakeDialogProgress.cancel_after
        if ca is not None and _FakeDialogProgress._polls >= ca:
            return True
        return False


# captured log lines so redaction tests can assert on them
LOG_LINES = []


def _make_xbmc():
    m = types.ModuleType("xbmc")
    m.LOGINFO = 1
    m.LOGWARNING = 2
    m.LOGERROR = 3
    m.LOGDEBUG = 0

    def _log(msg, level=1):
        LOG_LINES.append((level, msg))

    m.log = _log
    m.translatePath = lambda p: p  # PY2 path; unused here
    m._executed = []
    m.executebuiltin = lambda *a, **k: m._executed.append(a[0] if a else "")
    m.sleep = lambda *a, **k: None
    return m


def _make_xbmcaddon():
    m = types.ModuleType("xbmcaddon")
    m.Addon = _FakeAddon
    return m


def _make_xbmcgui():
    m = types.ModuleType("xbmcgui")
    m.Dialog = _FakeDialog
    m.DialogProgress = _FakeDialogProgress
    m.INPUT_ALPHANUM = 0
    m.NOTIFICATION_INFO = "info"
    m.NOTIFICATION_WARNING = "warning"
    m.NOTIFICATION_ERROR = "error"
    return m


class _FakeStat:
    """Stat over the _FakeFile payload store: st_size() reports the current bytes at the
    path (0 if unknown), so a copy's post-write size check is realizable. A path in
    _overrides forces a specific size (used to fake a short/mismatched copy)."""

    _overrides = {}  # path -> forced size

    def __init__(self, path):
        self._path = path

    def st_size(self):
        if self._path in _FakeStat._overrides:
            return _FakeStat._overrides[self._path]
        data = _FakeFile._payloads.get(self._path, b"")
        return len(data or b"")


class _FakeFile:
    """xbmcvfs.File fake: chunked readBytes(n) reads and buffered write()s.

    The read source AND write destination is the shared _payloads[path] store, so a file
    written here (e.g. a copy sidecar) can then be Stat'd and read back. write() returns
    a bool like the real API and can be forced False for a path via _write_fails.
    A no-arg readBytes() still returns all bytes, so older tests keep working.
    """

    _payloads = {}  # path -> bytes
    _write_fails = set()  # paths whose write() returns False

    def __init__(self, path, mode="r"):
        self.path = path
        self.mode = mode or "r"
        self._writing = "w" in self.mode or "a" in self.mode
        if self._writing:
            self._buf = bytearray()
        else:
            self._data = _FakeFile._payloads.get(path, b"") or b""
            self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def readBytes(self, n=None):
        data = self._data
        if n is None or n < 0:
            chunk = data[self._pos :]
            self._pos = len(data)
        else:
            chunk = data[self._pos : self._pos + n]
            self._pos += len(chunk)
        return bytearray(chunk)

    def write(self, data):
        if self.path in _FakeFile._write_fails:
            return False
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._buf.extend(data)
        return True

    def close(self):
        if self._writing:
            _FakeFile._payloads[self.path] = bytes(self._buf)
        return True


def _make_xbmcvfs():
    m = types.ModuleType("xbmcvfs")
    # translatePath maps special://temp/<x> to a real local temp path the test sets
    m._temp_map = {}

    def _translate(p):
        return m._temp_map.get(p, p)

    m.translatePath = _translate
    m.Stat = _FakeStat
    m.File = _FakeFile

    def _listdir(path):
        return m._listdir_result.get(path, ([], []))

    m._listdir_result = {}
    m.listdir = _listdir

    m._deleted = []

    def _delete(p):
        m._deleted.append(p)
        _FakeFile._payloads.pop(p, None)
        return True

    m.delete = _delete

    # copy spy: records (src, dst); result defaults True but can be scripted False (or a
    # callable) via _copy_result. On success it moves the payload so a later read sees it.
    m._copies = []
    m._copy_result = True

    def _copy(a, b):
        m._copies.append((a, b))
        res = m._copy_result
        res = res(a, b) if callable(res) else res
        if res and a in _FakeFile._payloads:
            _FakeFile._payloads[b] = _FakeFile._payloads[a]
        return res

    m.copy = _copy

    # rename spy: records (src, dst); moves the payload; result scriptable via
    # _rename_result (False forces the copy+delete finalize fallback).
    m._renames = []
    m._rename_result = True

    def _rename(a, b):
        m._renames.append((a, b))
        if not m._rename_result:
            return False
        if a in _FakeFile._payloads:
            _FakeFile._payloads[b] = _FakeFile._payloads.pop(a)
        return True

    m.rename = _rename

    # exists: payload-driven, then a permissive default so existing tests (which never
    # populate payloads) keep seeing files as present. ui tests set _exists_default=False
    # to get payload-accurate existence for their cleanup assertions.
    m._exists_default = True

    def _exists(p):
        if p in _FakeFile._payloads:
            return True
        return m._exists_default

    m.exists = _exists
    m.mkdir = lambda p: True
    m.rmdir = lambda p: True
    return m


# --------------------------------------------------------------------------- #
# Fake requests
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, content=b""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self._content = content

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        data = self._content
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


class FakeRequests:
    """Records every POST and returns scripted responses."""

    def __init__(self):
        self.calls = []  # list of dict(url, headers, data, json, stream, timeout)
        self.responder = None  # callable(call_index, call_dict) -> FakeResponse

    def post(self, url, headers=None, data=None, stream=False, timeout=None, json=None):
        call = {
            "url": url,
            "headers": dict(headers or {}),
            "data": data,
            "json": json,
            "stream": stream,
            "timeout": timeout,
        }
        idx = len(self.calls)
        self.calls.append(call)
        if self.responder is None:
            return FakeResponse(200, {})
        return self.responder(idx, call)


def _reset_fakes():
    """Reset all shared fake state between tests."""
    LOG_LINES.clear()
    _FakeAddon._settings = {}
    _FakeDialog.reset()
    _FakeDialogProgress.reset()
    _FakeFile._payloads = {}
    _FakeFile._write_fails = set()
    _FakeStat._overrides = {}


@pytest.fixture
def fake_kodi(monkeypatch):
    """Install fake kodi + requests modules; return a namespace handle."""
    _reset_fakes()

    xbmc = _make_xbmc()
    xbmcaddon = _make_xbmcaddon()
    xbmcgui = _make_xbmcgui()
    xbmcvfs = _make_xbmcvfs()
    fake_requests = FakeRequests()

    mods = {
        "xbmc": xbmc,
        "xbmcaddon": xbmcaddon,
        "xbmcgui": xbmcgui,
        "xbmcvfs": xbmcvfs,
        "requests": fake_requests,
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # Make `from resources.lib.modules._appauth import ...` resolvable but EMPTY
    # so dropbox_remote falls back to the settings-based key/secret (we don't want
    # the real baked secret in the test process). We register a stub package tree.
    for pkg in ("resources", "resources.lib", "resources.lib.modules"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            monkeypatch.setitem(sys.modules, pkg, m)
    # _appauth absent -> dropbox_remote's try/except import falls through to settings


    # Import the real module fresh
    sys.modules.pop("dropbox_remote", None)
    spec = importlib.util.spec_from_file_location(
        "dropbox_remote",
        ADDON_ROOT / "resources" / "lib" / "modules" / "dropbox_remote.py",
    )
    dbx = importlib.util.module_from_spec(spec)
    sys.modules["dropbox_remote"] = dbx
    spec.loader.exec_module(dbx)

    ns = types.SimpleNamespace(
        dbx=dbx,
        requests=fake_requests,
        xbmc=xbmc,
        xbmcvfs=xbmcvfs,
        xbmcgui=xbmcgui,
        addon=_FakeAddon,
        FakeResponse=FakeResponse,
        log_lines=LOG_LINES,
        FakeFile=_FakeFile,
    )
    # reset module bearer cache between tests
    dbx._cache["bearer"] = None
    dbx._cache["exp"] = 0
    dbx._cache["rt"] = None
    # APP_KEY is now hardcoded (public PKCE client_id); blank it so tests exercise the
    # 'dropbox_key' settings-fallback path they set up.
    dbx.APP_KEY = ""
    return ns


@pytest.fixture
def ui_mod(monkeypatch):
    """Import the real ui.py under fake Kodi modules; return a handle with the module and
    its fake xbmc* deps for driving/asserting dialogs, progress, and VFS copies."""
    _reset_fakes()

    xbmc = _make_xbmc()
    xbmcaddon = _make_xbmcaddon()
    xbmcgui = _make_xbmcgui()
    xbmcvfs = _make_xbmcvfs()
    xbmcvfs._exists_default = False  # payload-accurate exists for copy/cleanup tests

    mods = {
        "xbmc": xbmc,
        "xbmcaddon": xbmcaddon,
        "xbmcgui": xbmcgui,
        "xbmcvfs": xbmcvfs,
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("ui", None)
    spec = importlib.util.spec_from_file_location(
        "ui",
        ADDON_ROOT / "resources" / "lib" / "modules" / "ui.py",
    )
    ui = importlib.util.module_from_spec(spec)
    sys.modules["ui"] = ui
    spec.loader.exec_module(ui)

    return types.SimpleNamespace(
        ui=ui,
        xbmc=xbmc,
        xbmcgui=xbmcgui,
        xbmcvfs=xbmcvfs,
        addon=_FakeAddon,
        Dialog=_FakeDialog,
        DialogProgress=_FakeDialogProgress,
        FakeFile=_FakeFile,
        FakeStat=_FakeStat,
        log_lines=LOG_LINES,
    )
