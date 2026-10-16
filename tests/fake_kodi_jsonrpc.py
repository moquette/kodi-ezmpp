"""A JSON-RPC fake that answers the way Kodi does, unknown ids included.

The fake that let Clear recent channels ship its ``pvrmanager.enabled``
pause answered ``Settings.GetSettingValue`` with a value for any id, so code
reading a setting Kodi does not have looked like it worked. Kodi answers an
unknown or hidden setting id with ``Invalid params`` (-32602): ``SettingsOperations.cpp``
``GetSettingValue`` and ``SetSettingValue`` return ``InvalidParams`` when
``GetSetting`` finds nothing, and ``JSONRPCUtils.h`` defines it as -32602.
``Addons.*`` on an add-on that is not installed answers the same way.

Install it with ``install(monkeypatch, xbmc_module)``; every call is recorded
in ``calls`` as ``(method, params)`` and every addon state change in
``addon_history`` as ``(addonid, enabled)``.
"""

from __future__ import annotations

import json
from pathlib import Path

INVALID_PARAMS = {"code": -32602, "message": "Invalid params."}

# The setting ids a real Kodi 22 profile answers for, measured on the bench
# (tests/data/kodi22-setting-ids.txt, also the settings profile's catalog).
# Anything else is unknown, as it is in Kodi.
_CATALOG = Path(__file__).parent / "data" / "kodi22-setting-ids.txt"


def known_setting_ids():
    return {
        line.strip()
        for line in _CATALOG.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


class FakeKodiRPC:
    def __init__(self, settings=None, addons=None):
        if settings is None:
            settings = dict.fromkeys(known_setting_ids(), "")
        self.settings = dict(settings)
        self.addons = dict(addons or {})  # addonid -> enabled
        self.calls = []
        self.addon_history = []
        self.on_call = None  # optional hook(method, params) run before answering
        # (method, enabled) pairs answered with an error, to model a refusal
        self.fail_methods = set()

    def _error(self, req_id):
        return {"id": req_id, "jsonrpc": "2.0", "error": dict(INVALID_PARAMS)}

    def _ok(self, req_id, result):
        return {"id": req_id, "jsonrpc": "2.0", "result": result}

    def answer(self, method, params, req_id=1):
        self.calls.append((method, dict(params or {})))
        if self.on_call is not None:
            self.on_call(method, params or {})
        params = params or {}
        if method == "Settings.GetSettingValue":
            sid = params.get("setting")
            if sid not in self.settings:
                return self._error(req_id)
            return self._ok(req_id, {"value": self.settings[sid]})
        if method == "Settings.SetSettingValue":
            sid = params.get("setting")
            if sid not in self.settings:
                return self._error(req_id)
            self.settings[sid] = params.get("value")
            return self._ok(req_id, True)
        if method == "Addons.GetAddonDetails":
            aid = params.get("addonid")
            if aid not in self.addons:
                return self._error(req_id)
            return self._ok(
                req_id, {"addon": {"addonid": aid, "enabled": self.addons[aid]}}
            )
        if method == "Addons.SetAddonEnabled":
            aid = params.get("addonid")
            flag = params.get("enabled")
            if aid not in self.addons or (method, flag) in self.fail_methods:
                return self._error(req_id)
            self.addons[aid] = bool(flag)
            self.addon_history.append((aid, bool(flag)))
            return self._ok(req_id, "OK")
        return {
            "id": req_id,
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found."},
        }

    def execute(self, payload):
        req = json.loads(payload)
        return json.dumps(
            self.answer(req.get("method"), req.get("params"), req.get("id", 1))
        )


def install(monkeypatch, xbmc_module, **kw):
    rpc = FakeKodiRPC(**kw)
    monkeypatch.setattr(xbmc_module, "executeJSONRPC", rpc.execute)
    return rpc
