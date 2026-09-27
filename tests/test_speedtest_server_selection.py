"""The vendored speed test picks a genuinely nearby server, and imports clean
on the Python Kodi 22 bundles.

MEASURED 2026-09-26 on the office Fire TV (kodi.log): the client geolocated to
Sacramento (lat 38.6711, lon -121.1495) and the test ran against "Megacable
(Huehuetoca) [2971.55 km]", ping 117 ms. Cause: speedtest.net's legacy XML
server lists now answer with ten servers chosen server-side with no regard to
the client's position (from this Mac, with the module's own User-Agent, eight
of the ten were in Denver), so the module's distance sort could only rank
those ten. The fix asks the JSON API, which takes the client's coordinates,
first; the XML lists are the fallback.

The same run printed two DeprecationWarnings at error level under Python
3.14: datetime.utcnow() and threading.Event.isSet(). The import here runs
with DeprecationWarning promoted to an error so neither can come back.
"""

import importlib.util
import io
import json
import sys
import types
import warnings
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "script.ezmaintenanceplusplus"
    / "resources"
    / "lib"
    / "modules"
    / "speedtest.py"
)
MODULES_DIR = MODULE_PATH.parent

SACRAMENTO = (38.6711, -121.1495)

# The JSON API's shape, verbatim keys, distances as the API reports them.
JSON_SERVERS = [
    {
        "url": "http://speedtest2.ca.mycci.net:8080/speedtest/upload.php",
        "lat": "38.7483",
        "lon": "-121.2832",
        "distance": 9,
        "name": "Roseville, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "Fidium",
        "id": "72355",
        "host": "speedtest2.ca.mycci.net:8080",
    },
    {
        "url": "http://speedtest.zetabroadband.com:8080/speedtest/upload.php",
        "lat": "38.5031",
        "lon": "-121.0847",
        "distance": 12,
        "name": "Rancho Murieta, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "Zeta Broadband",
        "id": "7977",
        "host": "speedtest.zetabroadband.com:8080",
    },
    {
        "url": "http://stosat-scrm-01.sys.comcast.net:8080/speedtest/upload.php",
        "lat": "38.5816",
        "lon": "-121.4944",
        "distance": 19,
        "name": "Sacramento, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "Comcast",
        "id": "9436",
        "host": "stosat-scrm-01.sys.comcast.net:8080",
    },
    {
        "url": "http://speedtest.softcom.net:8080/speedtest/upload.php",
        "lat": "38.2546",
        "lon": "-121.2999",
        "distance": 30,
        "name": "Galt, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "Softcom",
        "id": "3801",
        "host": "speedtest.softcom.net:8080",
    },
    {
        "url": "http://speedtest.caltel.com:8080/speedtest/upload.php",
        "lat": "37.9829",
        "lon": "-120.6427",
        "distance": 55,
        "name": "Copperopolis, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "CalTel",
        "id": "68304",
        "host": "speedtest.caltel.com:8080",
    },
    {
        "url": "http://speedtest-conc.waveip.org:8080/speedtest/upload.php",
        "lat": "37.9780",
        "lon": "-122.0311",
        "distance": 68,
        "name": "Concord, CA",
        "country": "United States",
        "cc": "US",
        "sponsor": "Astound Broadband",
        "id": "72158",
        "host": "speedtest-conc.waveip.org:8080",
    },
]

# What the legacy XML list handed a Sacramento client: a far batch.
LEGACY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<settings>
<servers>
<server url="http://speedtest.megacable.com.mx:8080/speedtest/upload.php" lat="19.8333" lon="-99.2000" name="Huehuetoca" country="Mexico" cc="MX" sponsor="Megacable" id="40000" host="speedtest.megacable.com.mx:8080" />
<server url="http://speedtest.denver.example:8080/speedtest/upload.php" lat="39.7392" lon="-104.9903" name="Denver, CO" country="United States" cc="US" sponsor="Example" id="40001" host="speedtest.denver.example:8080" />
</servers>
</settings>
"""


class _FakeResponse:
    def __init__(self, body, code=200):
        self._buf = io.BytesIO(body)
        self.code = code
        self.headers = {}

    def getheader(self, name):
        return None

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        return None


class _FakeHTTPError(Exception):
    pass


def _load_module(monkeypatch):
    """Import the real speedtest.py with Kodi stubbed, DeprecationWarning fatal."""
    gui = types.ModuleType("xbmcgui")

    class DialogProgress:
        def create(self, *a, **k):
            return None

        def update(self, *a, **k):
            return None

        def close(self):
            return None

    class WindowDialog:
        pass

    gui.DialogProgress = DialogProgress
    gui.WindowDialog = WindowDialog
    monkeypatch.setitem(sys.modules, "xbmcgui", gui)
    monkeypatch.setitem(sys.modules, "xbmcaddon", types.ModuleType("xbmcaddon"))
    monkeypatch.syspath_prepend(str(MODULES_DIR))
    sys.modules.pop("ezm_speedtest_under_test", None)

    spec = importlib.util.spec_from_file_location(
        "ezm_speedtest_under_test", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    # The module wraps sys.stdout.fileno() in a FileIO at import; under pytest
    # that is the capture fd, and the wrapper closes it when collected. A
    # StringIO has no fileno, so the module takes its own "use it as is" branch.
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            spec.loader.exec_module(mod)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    return mod


@pytest.fixture
def st(monkeypatch):
    return _load_module(monkeypatch)


def _speedtest(mod, monkeypatch, responder):
    """A Speedtest with the config step replaced and HTTP answered by responder."""

    def fake_get_config(self):
        self.config.update(
            {
                "client": {"lat": str(SACRAMENTO[0]), "lon": str(SACRAMENTO[1])},
                "ignore_servers": [],
                "threads": {"download": 8, "upload": 2},
            }
        )
        self.lat_lon = SACRAMENTO
        return self.config

    monkeypatch.setattr(mod.Speedtest, "get_config", fake_get_config)
    monkeypatch.setattr(mod, "build_opener", lambda *a, **k: object())
    fetched = []

    def fake_catch_request(request, opener=None):
        fetched.append(request.full_url)
        try:
            return responder(request.full_url), False
        except _FakeHTTPError as e:
            return None, e

    monkeypatch.setattr(mod, "catch_request", fake_catch_request)
    s = mod.Speedtest()
    s.fetched = fetched
    return s


def _json_then_xml(json_code=200):
    def responder(url):
        if "/api/js/servers" in url:
            if json_code != 200:
                raise _FakeHTTPError("HTTP %d" % json_code)
            assert "lat=38.6711" in url and "lon=-121.1495" in url, url
            return _FakeResponse(json.dumps(JSON_SERVERS).encode("utf-8"))
        return _FakeResponse(LEGACY_XML)

    return responder


def test_module_imports_and_runs_with_deprecations_fatal(st):
    """The two warnings the office box printed (utcnow, isSet) are gone, and the
    paths that raised them run clean under a fatal DeprecationWarning filter."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        results = st.SpeedtestResults(opener=object())
        assert results.timestamp.endswith("Z")
        assert "T" in results.timestamp
        callback = st.print_dots(st.FakeShutdownEvent())
        callback(0, 1)
    assert not hasattr(st.FakeShutdownEvent, "isSet")
    assert st.FakeShutdownEvent.is_set() is False
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "isSet(" not in source
    assert "utcnow(" not in source
    assert "getiterator(" not in source


def test_json_api_is_asked_with_client_coordinates_and_wins(st, monkeypatch):
    s = _speedtest(st, monkeypatch, _json_then_xml())
    s.get_servers()
    assert s.fetched, "no server list was fetched"
    assert "/api/js/servers" in s.fetched[0]
    assert len(s.fetched) == 1, "the XML fallback must not run when JSON answered"
    closest = s.get_closest_servers()
    assert [c["id"] for c in closest] == ["72355", "7977", "9436", "3801", "68304"]
    assert all(c["d"] < 100 for c in closest)


def test_nearest_healthy_server_wins_when_the_nearest_is_down(st, monkeypatch):
    """Roseville (9 km) answers garbage to the latency probe; Rancho Murieta
    (12 km) is the nearest healthy server and must be the pick, not Huehuetoca."""
    s = _speedtest(st, monkeypatch, _json_then_xml())
    s.get_servers()

    class FakeClock:
        now = 0.0

        def default_timer(self):
            return self.now

    clock = FakeClock()
    latency_by_host = {
        "speedtest2.ca.mycci.net:8080": None,  # down
        "speedtest.zetabroadband.com:8080": 0.012,
        "stosat-scrm-01.sys.comcast.net:8080": 0.019,
        "speedtest.softcom.net:8080": 0.030,
        "speedtest.caltel.com:8080": 0.055,
    }

    class FakeConn:
        def __init__(self, host, source_address=None):
            self.host = host

        def request(self, method, path, headers=None):
            lat = latency_by_host[self.host]
            self.ok = lat is not None
            clock.now += lat if lat is not None else 0.0

        def getresponse(self):
            r = types.SimpleNamespace()
            r.status = 200 if self.ok else 500
            r.read = lambda n: b"test=test" if self.ok else b"<html>"
            return r

        def close(self):
            return None

    fake_timeit = types.SimpleNamespace(
        default_timer=clock.default_timer, time=types.SimpleNamespace(time=lambda: 0)
    )
    monkeypatch.setattr(st, "timeit", fake_timeit)
    monkeypatch.setattr(st, "SpeedtestHTTPConnection", FakeConn)
    best = s.get_best_server()
    assert best["id"] == "7977"
    assert best["name"] == "Rancho Murieta, CA"
    assert best["d"] < 20
    assert best["latency"] == pytest.approx(6.0)  # 3 probes of 12 ms over 6


def test_xml_list_is_the_fallback_when_json_api_refuses(st, monkeypatch):
    """A 403 from the JSON API (what a browser User-Agent gets) falls back to
    the legacy XML list, parsed by ElementTree (minidom only when ElementTree
    cannot be imported, see the parser-parity tests below)."""
    s = _speedtest(st, monkeypatch, _json_then_xml(json_code=403))
    s.get_servers()
    assert any("/api/js/servers" in u for u in s.fetched)
    assert any("speedtest-servers" in u for u in s.fetched)
    ids = sorted(int(x["id"]) for v in s.servers.values() for x in v)
    assert ids == [40000, 40001]
    closest = s.get_closest_servers()
    assert closest[0]["name"] == "Denver, CO"


def test_json_filters_honour_exclude_and_ignore(st, monkeypatch):
    s = _speedtest(st, monkeypatch, _json_then_xml())
    s.config["ignore_servers"] = [72355]
    s.get_servers(exclude=[7977])
    ids = [x["id"] for v in sorted(s.servers.items()) for x in v[1]]
    assert ids[0] == "9436"
    assert "72355" not in ids and "7977" not in ids


def test_candidates_are_logged_for_kodi_log(st, monkeypatch, capsys):
    s = _speedtest(st, monkeypatch, _json_then_xml())
    s.get_servers()
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if ln.startswith("Speedtest servers from")]
    assert len(line) == 1
    assert "72355 Fidium (Roseville, CA) 14 km" in line[0]


# --------------------------------------------------------------------------- #
# ElementTree is the parser again; minidom is the ImportError fallback only
# --------------------------------------------------------------------------- #
# Captured 2026-09-26 from this Mac with the module's own User-Agent shape
# ("Mozilla/5.0 (Darwin; U; 64bit; en-us) Python/3.14.0 (KHTML, like Gecko)
# speedtest-cli/2.0.0"): speedtest-config.php (HTTP 200, 7055 bytes) and
# speedtest-servers-static.php?threads=4 (HTTP 200, 2461 bytes, 10 servers).
# No credentials are involved; the client ip attribute was replaced with the
# documentation address 203.0.113.7 because this repository is public.
DATA = Path(__file__).resolve().parent / "data"
CONFIG_XML = (DATA / "speedtest-config.xml").read_bytes()
SERVERS_XML = (DATA / "speedtest-servers-static.xml").read_bytes()


def test_elementtree_is_live_and_minidom_is_only_the_import_fallback(st):
    assert st.ET is not None, "ElementTree must be the parser on Python 3.14"
    assert st.DOM is None, "minidom is imported only when ElementTree is not"
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert source.count("ET = None") == 1, "ET may be nulled only on ImportError"


def _config_via(mod, monkeypatch, use_minidom):
    if use_minidom:
        from xml.dom import minidom

        monkeypatch.setattr(mod, "ET", None)
        monkeypatch.setattr(mod, "DOM", minidom)
    monkeypatch.setattr(mod, "build_opener", lambda *a, **k: object())
    monkeypatch.setattr(
        mod,
        "catch_request",
        lambda request, opener=None: (_FakeResponse(CONFIG_XML), False),
    )
    s = mod.Speedtest()
    return s.config, s.lat_lon


def _servers_via(mod, monkeypatch, use_minidom):
    if use_minidom:
        from xml.dom import minidom

        monkeypatch.setattr(mod, "ET", None)
        monkeypatch.setattr(mod, "DOM", minidom)

    def responder(url):
        if "/api/js/servers" in url:
            raise _FakeHTTPError("HTTP 403")
        return _FakeResponse(SERVERS_XML)

    s = _speedtest(mod, monkeypatch, responder)
    s.get_servers()
    return s.servers


def test_config_parser_agrees_on_elementtree_and_minidom(monkeypatch):
    et = _config_via(_load_module(monkeypatch), monkeypatch, use_minidom=False)
    dom = _config_via(_load_module(monkeypatch), monkeypatch, use_minidom=True)
    assert et == dom
    config, lat_lon = et
    assert config["client"]["ip"] == "203.0.113.7"
    assert config["client"]["isp"] == "AT&T Internet"
    assert config["threads"] == {"upload": 2, "download": 8}
    assert lat_lon == (32.9567, -97.336)
    assert 683 in config["ignore_servers"]


def test_server_list_parser_agrees_on_elementtree_and_minidom(monkeypatch):
    et = _servers_via(_load_module(monkeypatch), monkeypatch, use_minidom=False)
    dom = _servers_via(_load_module(monkeypatch), monkeypatch, use_minidom=True)
    assert et == dom
    ids = sorted(int(x["id"]) for v in et.values() for x in v)
    assert len(ids) == 10
    assert 60813 in ids and 12009 in ids


def test_a_malformed_config_is_a_config_error_not_a_crash(st, monkeypatch):
    monkeypatch.setattr(st, "build_opener", lambda *a, **k: object())
    monkeypatch.setattr(
        st,
        "catch_request",
        lambda request, opener=None: (_FakeResponse(b"<settings><client"), False),
    )
    with pytest.raises(st.ConfigRetrievalError):
        st.Speedtest()


# --------------------------------------------------------------------------- #
# run(): the in-process entry point default.py calls
# --------------------------------------------------------------------------- #
PLUGIN_ARGV = ["plugin://script.ezmaintenanceplusplus/", "7", "?action=speedtest"]


def _recording_dialog(mod, monkeypatch):
    log = []

    class DialogProgress:
        def create(self, *a, **k):
            log.append("create")

        def update(self, *a, **k):
            return None

        def close(self):
            log.append("close")

    monkeypatch.setattr(mod.xbmcgui, "DialogProgress", DialogProgress)
    return log


def test_importing_the_module_opens_no_dialog(st):
    assert st.dp is None


def test_run_owns_its_dialog_and_ignores_the_plugin_argv(st, monkeypatch):
    log = _recording_dialog(st, monkeypatch)
    monkeypatch.setattr(sys, "argv", PLUGIN_ARGV)
    seen = []
    monkeypatch.setattr(st, "main", lambda argv=None: seen.append(argv))
    assert st.run() is True
    assert seen == [[]], "run must hand main an explicit, empty argv"
    assert log == ["create", "close"]
    assert st.dp is None, "the dialog must not outlive the run"


def test_run_returns_on_failure_with_the_dialog_closed(st, monkeypatch):
    log = _recording_dialog(st, monkeypatch)

    def boom(argv=None):
        raise SystemExit("ERROR: Cannot retrieve speedtest configuration")

    monkeypatch.setattr(st, "main", boom)
    real_err = sys.stderr
    sys.stderr = io.StringIO()
    try:
        assert st.run() is False
    finally:
        sys.stderr = real_err
    assert log == ["create", "close"]
    assert st.dp is None


def test_the_real_parser_accepts_an_empty_argv_under_a_plugin_sys_argv(
    st, monkeypatch
):
    """Under the plugin invoker sys.argv is the plugin URL, handle and query;
    parse_args() with no argv would exit(2) on them."""
    monkeypatch.setattr(sys, "argv", PLUGIN_ARGV)
    args = st.parse_args([])
    assert args.share is True and args.download is True and args.upload is True
