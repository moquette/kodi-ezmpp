"""
EZ Maintenance++ : Dropbox destination (requests-only, NO SDK).

Talks to the Dropbox v2 REST API directly with script.module.requests so we add no
new dependency and vendor nothing. The add-on is registered as a Dropbox App-folder
app, so every path here is IN-APP-RELATIVE ("/name.zip", "" = the app folder root):
Dropbox scopes us to Apps/<app>/ and we must never send "/Apps/...".

Secrets contract: the App key/secret are baked into the constants below for the
one-tap experience; if left empty they fall back to the (whitespace-trimmed) settings
'dropbox_key'/'dropbox_secret'. The long-lived refresh token lives only in the setting
'dropbox_refresh_token'. None of key / secret / code / token is ever logged.

This program is free software: GPL v3 or later (see the other modules).
"""

import base64
import hashlib
import json
import os
import re
import time

import requests
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from resources.lib.modules import addon_id as _addon_id

# Backup filenames carry a trailing _YYYYMMDDHHMM stamp before ".zip"
# (the 12-digit datetime EZ Maintenance++ appends at backup time).
_STAMP_RE = re.compile(r"_(\d{12})\.zip$", re.IGNORECASE)


def _name_stamp(name):
    """Parse the trailing _YYYYMMDDHHMM stamp from a backup filename.

    Returns the 12-digit string (lexically == chronologically sortable) or ""
    when no stamp is present, so an unstamped file sorts as the OLDEST and can
    never cause a newer, stamped file to be ranked below it.
    """
    m = _STAMP_RE.search(name or "")
    return m.group(1) if m else ""


# Built-in Dropbox App-folder client_id for the "tony-7-backup" app. Under PKCE there is
# NO client_secret, and an OAuth client_id is public by design (it ships in every copy of
# the add-on), so it is safe to hardcode here. Falls back to the 'dropbox_key' advanced
# setting if a user points the add-on at their own Dropbox app.
APP_KEY = "0xzoqv9xq7ji6et"
APP_SECRET = (
    ""  # unused under PKCE; kept only for the legacy _secret() settings fallback
)

# Upload-session chunk size: a small 8 MiB (a multiple of Dropbox's 4 MiB session
# unit) so one chunk finishes well inside TIMEOUT even on a slow Fire TV / Apple TV
# wifi uplink. The old 50 MB chunk could not finish a single socket write before the
# timeout, and with no per-chunk resume one slow chunk killed the whole upload. See
# _do_upload + _session_append for the resumable retry that pairs with this.
CHUNK = 8 * 1024 * 1024
OAUTH_AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
OAUTH_TOKEN = "https://api.dropboxapi.com/oauth2/token"
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"

# (connect, read) timeout per request. Generous (each chunk streams from disk) but
# far shorter than before: a stalled connection now fails fast so the chunk is
# retried/resumed instead of hanging for minutes.
TIMEOUT = (10, 180)

# Per-chunk attempts before a session upload gives up (upload() then retries the
# whole op once as a final backstop). Transient/network failures back off. Set high
# so a brief stall (e.g. a TV screensaver suspending the app) is ridden out by
# resuming the same chunk rather than restarting the whole upload.
MAX_TRIES = 8

AddonID = _addon_id.get()
AddonTitle = "EZ Maintenance++"

_addon = xbmcaddon.Addon(id=AddonID)

# module-level in-memory bearer cache; exp is an absolute unix time, rt is the refresh
# token (account) the bearer was minted for so a re-auth to another account is not served
# a stale bearer.
_cache = {"bearer": None, "exp": 0, "rt": None}


class DropboxAuthError(Exception):
    pass


class DropboxCanceled(Exception):
    """Raised when an upload progress callback asks to cancel the in-flight upload."""

    pass


def _log(msg):
    # NEVER pass a token / code / key / secret in here.
    xbmc.log("%s [dropbox] %s" % (AddonTitle, msg), level=xbmc.LOGINFO)


def _setting(key):
    try:
        return (xbmcaddon.Addon(id=AddonID).getSetting(key) or "").strip()
    except Exception:
        return ""


def _key():
    return APP_KEY if APP_KEY else _setting("dropbox_key")


def _secret():
    return APP_SECRET if APP_SECRET else _setting("dropbox_secret")


def _refresh_token():
    return _setting("dropbox_refresh_token")


def _set_refresh_token(token):
    xbmcaddon.Addon(id=AddonID).setSetting("dropbox_refresh_token", token)


def _pkce_pair():
    """Return (code_verifier, code_challenge) for a PKCE S256 sign-in. The verifier is
    a fresh per-sign-in secret that never leaves the device; the challenge is its
    SHA-256 (base64url, unpadded) and is the only PKCE value that travels in the URL."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize():
    """Interactive one-time sign in via PKCE: show a QR, take the code, swap it for a
    refresh token using the code_verifier (NO client_secret), and store it."""
    key = _key()
    if not key:
        xbmcgui.Dialog().ok(
            AddonTitle,
            "Dropbox is not set up in this build. Open the add-on settings and paste a "
            "Dropbox App key (Advanced), then try Sign in again.",
        )
        return False

    verifier, challenge = _pkce_pair()
    auth_url = (
        "%s?client_id=%s&response_type=code&code_challenge=%s"
        "&code_challenge_method=S256&token_access_type=offline"
        % (OAUTH_AUTHORIZE, key, challenge)
    )
    _show_auth_prompt(auth_url)
    code = xbmcgui.Dialog().input("Enter the Dropbox code", type=xbmcgui.INPUT_ALPHANUM)
    if not code:
        return False
    code = code.strip()

    try:
        resp = requests.post(
            OAUTH_TOKEN,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": key,
                "code_verifier": verifier,
            },
            timeout=TIMEOUT,
        )
    except Exception as e:
        _log("authorize: token request failed: %s" % type(e).__name__)
        xbmcgui.Dialog().ok(
            AddonTitle, "Could not reach Dropbox. Check the network and try again."
        )
        return False

    if resp.status_code != 200:
        _log("authorize: token exchange rejected (HTTP %s)" % resp.status_code)
        xbmcgui.Dialog().ok(
            AddonTitle,
            "Dropbox did not accept that code. Make sure you copied the whole code and try Sign in again.",
        )
        return False

    data = resp.json()
    refresh = data.get("refresh_token", "")
    if not refresh:
        _log("authorize: response had no refresh_token")
        xbmcgui.Dialog().ok(
            AddonTitle, "Dropbox did not return a refresh token. Try Sign in again."
        )
        return False

    _set_refresh_token(refresh)
    # warm the bearer cache off the access token we just got
    expires_in = int(data.get("expires_in", 14400))
    bearer = data.get("access_token")
    if bearer:
        _cache["bearer"] = bearer
        _cache["rt"] = refresh
        _cache["exp"] = time.time() + expires_in
    _log("authorize: connected (refresh token stored)")
    xbmcgui.Dialog().ok(AddonTitle, "Connected to Dropbox.")
    return True


def _show_auth_prompt(url):
    """Show a scannable QR of the PKCE authorize URL so nobody types it on a remote.

    The QR is generated locally on the device (no external service), which is required
    under PKCE because the URL carries a per-sign-in challenge and so cannot be
    pre-baked. If QR generation or the window fails, it falls back to the plain URL
    dialog. Only the public client_id and the PKCE challenge ride in the URL - no
    secret, code, or token."""
    try:
        img = _qr_image(url)
        if img:
            _make_qr_window(img).doModal()
            return
    except Exception as e:
        _log("auth QR unavailable, showing URL instead: %s" % type(e).__name__)
    xbmcgui.Dialog().ok(
        AddonTitle,
        "On a phone or computer, open this URL:\n\n%s\n\n"
        "Approve access, then enter the code Dropbox shows on the next screen." % url,
    )


_qr_seq = 0


def _qr_image(url):
    """Generate a QR PNG for `url` locally (no network) and return a Kodi special://
    path for it. Uses the vendored encoder + zlib PNG writer in _qrgen.

    Two hard-won rules baked in here (both cost a blank barcode on Kodi 21.3/tvOS+Fire TV):
    - FRESH filename every call. Kodi caches textures by path, so reusing one name makes a
      first failed load stick as a blank even after the file is rewritten - which is exactly
      what happened when Kodi refused the old grayscale PNG, cached the failure for
      _dbx_qr.png, and kept drawing blank even after the 32-bit fix until a restart. A unique
      name always loads fresh (no restart needed).
    - Hand ControlImage the special:// path, NOT the translatePath'd absolute path. Kodi's
      texture loader resolves special:// through its own VFS and loads it reliably across
      platforms; a raw device path (e.g. /storage/emulated/0/... on Fire TV) is fragile."""
    from resources.lib.modules import _qrgen

    global _qr_seq
    _qr_seq += 1
    png = _qrgen.make_qr_png_bytes(url)
    special = "special://temp/_dbx_qr_%d_%d.png" % (int(time.time()), _qr_seq)
    # Write THROUGH xbmcvfs (Kodi's own VFS), NEVER plain open(). On Apple TV (tvOS)
    # Kodi's VFS - which its texture loader uses to READ - silently reads EMPTY for a
    # local file written by a *different* writer (plain Python open()); it's an App
    # Sandbox scoped-resource quirk, documented in
    # docs/playbooks/kodi-vfs-cannot-read-foreign-local-files.md (same repo). That is why
    # the barcode was blank on Apple TV (plain-open PNG) but fine on Fire TV (Android, no
    # such restriction). Writing THROUGH xbmcvfs makes the file "known" to Kodi so the
    # texture loader can read it back. special://temp (not userdata) => no NSUserDefaults
    # mirroring. Fresh filename each call defeats Kodi's per-path texture cache; the PNG is
    # already 32-bit so Kodi 21.3 will draw it; return the special:// path for ControlImage.
    f = xbmcvfs.File(special, "w")
    try:
        ok = f.write(bytearray(png))
    finally:
        f.close()
    if not ok:
        _log("authorize: QR write via xbmcvfs failed")
    return special


def _make_qr_window(image_path):
    """Build the fullscreen QR overlay. Defined lazily so importing this module under
    a mock Kodi (tests) never needs xbmcgui.WindowDialog."""

    class _QRWindow(xbmcgui.WindowDialog):
        def __init__(self):
            w, h, qr = 1280, 720, 480
            try:
                bg = os.path.join(
                    xbmcaddon.Addon(id=AddonID).getAddonInfo("path"),
                    "resources",
                    "skins",
                    "Default",
                    "media",
                    "bg-fade.png",
                )
                self.addControl(xbmcgui.ControlImage(0, 0, w, h, bg))
            except Exception:
                pass
            self.addControl(xbmcgui.ControlImage((w - qr) // 2, 60, qr, qr, image_path))
            self.addControl(
                xbmcgui.ControlLabel(
                    0,
                    560,
                    w,
                    40,
                    "Scan with your phone, approve, then press OK to enter the code",
                    alignment=(0x00000002 | 0x00000004),
                )
            )

        def onAction(self, action):
            # close on OK/select or any back/menu/stop action
            if action.getId() in (7, 9, 10, 13, 92):
                self.close()

    return _QRWindow()


def _access_token(force=False):
    """Return a valid bearer token, refreshing via the stored refresh token as needed.

    The cached bearer is bound to the account it was minted for (its refresh token). If
    the signed-in account changed (the stored refresh token differs), the cache is NOT
    reused - otherwise the add-on would keep reading the OLD account's Dropbox folder
    until the token expired, so a list/restore would show a stale account. Binding the
    cache to the account keeps every listing a live read of the account you are actually
    signed into. (`rt is None` means a bearer set outside this function - e.g. tests or
    authorize's warm - which is already the current account, so it is honored.)
    """
    now = time.time()
    refresh = _refresh_token()
    if not refresh:
        raise DropboxAuthError("not signed in to Dropbox")
    if (
        not force
        and _cache["bearer"]
        and now < (_cache["exp"] - 300)
        and _cache.get("rt") in (None, refresh)
    ):
        return _cache["bearer"]
    key = _key()
    if not key:
        raise DropboxAuthError("Dropbox app key missing")

    try:
        resp = requests.post(
            OAUTH_TOKEN,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": key,
            },
            timeout=TIMEOUT,
        )
    except Exception as e:
        raise DropboxAuthError("token refresh network error: %s" % type(e).__name__)

    if resp.status_code == 400:
        _log("access_token: refresh rejected (HTTP 400)")
        raise DropboxAuthError("Dropbox refresh token rejected; sign in again")
    if resp.status_code != 200:
        raise DropboxAuthError("token refresh failed (HTTP %s)" % resp.status_code)

    data = resp.json()
    bearer = data.get("access_token")
    if not bearer:
        raise DropboxAuthError("token refresh returned no access_token")
    _cache["bearer"] = bearer
    _cache["rt"] = refresh
    _cache["exp"] = now + int(data.get("expires_in", 14400))
    return bearer


def _auth_header(force=False):
    return {"Authorization": "Bearer %s" % _access_token(force=force)}


def _handle_retryable(resp):
    """Return True if the caller should retry once. Handles 401 (refresh) and 429 (sleep)."""
    if resp.status_code == 401:
        _access_token(force=True)
        return True
    if resp.status_code == 429:
        try:
            wait = int(resp.headers.get("Retry-After", "1")) + 1
        except Exception:
            wait = 2
        _log("rate limited; sleeping %ss" % wait)
        time.sleep(wait)
        return True
    return False


def _rpc(url, arg, force=False):
    """A JSON-body Dropbox RPC endpoint (api.dropboxapi.com)."""
    headers = _auth_header(force=force)
    headers["Content-Type"] = "application/json"
    return requests.post(url, headers=headers, data=json.dumps(arg), timeout=TIMEOUT)


# ----------------------------------------------------------------------------- upload


def upload(local_path, remote_name, progress=None):
    """Upload a local file to /<remote_name> in the app folder.

    Small files (<= CHUNK) go in one streamed request; large files use a resumable
    upload session where every chunk is retried with backoff and the loop resumes
    from the session offset on failure. The whole op is retried once as a backstop.

    `progress`, if given, is called progress(bytes_sent, total_bytes) after each
    chunk; returning False cancels (raises DropboxCanceled, which is never retried).
    """
    last_err = None
    for attempt in (1, 2):
        try:
            _do_upload(local_path, remote_name, progress=progress)
            return True
        except DropboxCanceled:
            raise
        except Exception as e:
            last_err = e
            _log("upload attempt %s failed: %s" % (attempt, type(e).__name__))
    if last_err:
        raise last_err
    return False


def _do_upload(local_path, remote_name, progress=None):
    path = "/" + remote_name
    abs_local = xbmcvfs.translatePath(local_path)
    size = xbmcvfs.Stat(local_path).st_size()

    def _report(sent):
        if progress is not None and progress(sent, size) is False:
            raise DropboxCanceled()

    if size <= CHUNK:
        _report(0)
        arg = {"path": path, "mode": "overwrite", "mute": True}
        for attempt in (1, 2):
            headers = _auth_header(force=(attempt == 2))
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            headers["Content-Type"] = "application/octet-stream"
            # Stream from disk - pass the open file object so requests sends it
            # chunk by chunk and never buffers the whole (<=CHUNK) file in RAM.
            # Re-open per attempt so a 401/429 retry restarts from byte 0.
            with open(abs_local, "rb") as fh:
                resp = requests.post(
                    CONTENT + "/files/upload",
                    headers=headers,
                    data=fh,
                    timeout=TIMEOUT,
                )
            if resp.status_code == 200:
                _report(size)
                return
            if attempt == 1 and _handle_retryable(resp):
                continue
            raise DropboxAuthError("upload failed (HTTP %s)" % resp.status_code)
        return

    # large file: resumable chunked upload session, streaming from disk one CHUNK at
    # a time. Each chunk retries on its own and the loop resumes from the session
    # offset, so one slow/timed-out chunk no longer restarts the whole file.
    with open(abs_local, "rb") as fh:
        _report(0)
        session_id, offset = _session_start(fh)
        _report(offset)
        while offset < size:
            offset = _session_append(session_id, fh, offset)
            _report(offset)
    _session_finish(session_id, offset, path)
    _report(size)


def _is_transient(resp):
    """A server-side hiccup worth retrying with backoff (vs a hard 4xx)."""
    return resp.status_code in (500, 502, 503, 504)


def _correct_offset(resp):
    """If resp is Dropbox's 409 incorrect_offset, return the offset Dropbox actually
    holds (so the session can resume there); otherwise None."""
    if resp.status_code != 409:
        return None
    try:
        err = resp.json().get("error", {})
        if isinstance(err, dict) and err.get(".tag") == "incorrect_offset":
            return int(err.get("correct_offset"))
    except Exception:
        return None
    return None


def _session_start(fh):
    """Open an upload session with the first chunk. Returns (session_id, bytes_sent).
    Retries transient/network failures, re-reading the first chunk from disk."""
    last = None
    delay = 1
    for attempt in range(1, MAX_TRIES + 1):
        fh.seek(0)
        data = fh.read(CHUNK)
        try:
            headers = _auth_header()
            headers["Dropbox-API-Arg"] = json.dumps({"close": False})
            headers["Content-Type"] = "application/octet-stream"
            resp = requests.post(
                CONTENT + "/files/upload_session/start",
                headers=headers,
                data=data,
                timeout=TIMEOUT,
            )
        except Exception as e:
            last = e
            _log(
                "session/start net error (try %s/%s): %s"
                % (attempt, MAX_TRIES, type(e).__name__)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 200:
            return resp.json()["session_id"], len(data)
        if _handle_retryable(resp):
            continue
        if _is_transient(resp):
            _log(
                "session/start transient HTTP %s (try %s/%s)"
                % (resp.status_code, attempt, MAX_TRIES)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        raise DropboxAuthError(
            "upload_session/start failed (HTTP %s)" % resp.status_code
        )
    raise DropboxAuthError(
        "upload_session/start failed after %s attempts%s"
        % (MAX_TRIES, (": %s" % type(last).__name__) if last else "")
    )


def _session_append(session_id, fh, offset):
    """Upload one chunk starting at `offset`. Retries transient/network failures
    (re-reading from disk) and, on Dropbox's incorrect_offset, resumes from the
    offset Dropbox reports. Returns the new absolute offset."""
    last = None
    delay = 1
    for attempt in range(1, MAX_TRIES + 1):
        fh.seek(offset)
        data = fh.read(CHUNK)
        if not data:
            return offset
        arg = {"cursor": {"session_id": session_id, "offset": offset}, "close": False}
        try:
            headers = _auth_header()
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            headers["Content-Type"] = "application/octet-stream"
            resp = requests.post(
                CONTENT + "/files/upload_session/append_v2",
                headers=headers,
                data=data,
                timeout=TIMEOUT,
            )
        except Exception as e:
            last = e
            _log(
                "session/append net error at %s (try %s/%s): %s"
                % (offset, attempt, MAX_TRIES, type(e).__name__)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 200:
            return offset + len(data)
        co = _correct_offset(resp)
        if co is not None:
            # A prior (maybe timed-out) attempt already landed bytes; jump to the
            # offset Dropbox actually holds instead of re-sending what it has.
            _log("session/append resync %s -> %s" % (offset, co))
            return co
        if _handle_retryable(resp):
            continue
        if _is_transient(resp):
            _log(
                "session/append transient HTTP %s at %s (try %s/%s)"
                % (resp.status_code, offset, attempt, MAX_TRIES)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        raise DropboxAuthError(
            "upload_session/append_v2 failed (HTTP %s)" % resp.status_code
        )
    raise DropboxAuthError(
        "upload_session/append_v2 failed after %s attempts at offset %s%s"
        % (MAX_TRIES, offset, (": %s" % type(last).__name__) if last else "")
    )


def _session_finish(session_id, offset, path):
    arg = {
        "cursor": {"session_id": session_id, "offset": offset},
        "commit": {"path": path, "mode": "overwrite", "mute": True},
    }
    last = None
    delay = 1
    for attempt in range(1, MAX_TRIES + 1):
        try:
            headers = _auth_header()
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            headers["Content-Type"] = "application/octet-stream"
            resp = requests.post(
                CONTENT + "/files/upload_session/finish",
                headers=headers,
                data=b"",
                timeout=TIMEOUT,
            )
        except Exception as e:
            last = e
            _log(
                "session/finish net error (try %s/%s): %s"
                % (attempt, MAX_TRIES, type(e).__name__)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if resp.status_code == 200:
            return
        if _handle_retryable(resp):
            continue
        if _is_transient(resp):
            _log(
                "session/finish transient HTTP %s (try %s/%s)"
                % (resp.status_code, attempt, MAX_TRIES)
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        raise DropboxAuthError(
            "upload_session/finish failed (HTTP %s)" % resp.status_code
        )
    raise DropboxAuthError(
        "upload_session/finish failed after %s attempts%s"
        % (MAX_TRIES, (": %s" % type(last).__name__) if last else "")
    )


# --------------------------------------------------------------------------- listing


def list_backups():
    """Return .zip names in the app folder root, newest first."""
    arg = {"path": "", "recursive": False}
    resp = _rpc(API + "/files/list_folder", arg)
    if _handle_retryable(resp):
        resp = _rpc(API + "/files/list_folder", arg, force=True)
    if resp.status_code != 200:
        raise DropboxAuthError("list_folder failed (HTTP %s)" % resp.status_code)
    data = resp.json()
    entries = data.get("entries", [])
    cursor = data.get("cursor")
    has_more = data.get("has_more", False)
    while has_more:
        resp = _rpc(API + "/files/list_folder/continue", {"cursor": cursor})
        if _handle_retryable(resp):
            resp = _rpc(
                API + "/files/list_folder/continue", {"cursor": cursor}, force=True
            )
        if resp.status_code != 200:
            raise DropboxAuthError(
                "list_folder/continue failed (HTTP %s)" % resp.status_code
            )
        data = resp.json()
        entries.extend(data.get("entries", []))
        cursor = data.get("cursor")
        has_more = data.get("has_more", False)

    files = []
    for e in entries:
        if e.get(".tag") == "file":
            n = e.get("name", "")
            if n.endswith(".zip"):
                files.append((n, e.get("server_modified", "")))
    # Newest first, ordered by the in-name _YYYYMMDDHHMM stamp (NOT raw lexical
    # name order: users name their backups, so prefixes differ and a name sort
    # could rank an older file above a newer one). Files with no parseable stamp
    # fall back to Dropbox's server_modified, then the raw name, and always sort
    # as the OLDEST so they can never displace a stamped, newer backup.
    files.sort(key=lambda t: (_name_stamp(t[0]), t[1], t[0]), reverse=True)
    return [n for n, _mod in files]


# ------------------------------------------------------------------------- download


def download(remote_name, dest_dir=None, progress=None):
    """Download /<remote_name> and return the path it was written to.

    By default it stages in special://temp; pass dest_dir (a real local directory) to
    stage elsewhere (the raw path is then returned). `progress`, if given, is called
    progress(bytes_received, total_bytes) as the stream is written; returning False
    cancels (the partial file is removed and DropboxCanceled is raised, so the caller
    reports "canceled" rather than "failed" and never restores a truncated zip)."""
    if dest_dir:
        dest = os.path.join(dest_dir, remote_name)
        ret = dest
    else:
        ret = "special://temp/" + remote_name
        dest = xbmcvfs.translatePath(ret)
    arg = {"path": "/" + remote_name}
    for attempt in (1, 2):
        headers = _auth_header(force=(attempt == 2))
        headers["Dropbox-API-Arg"] = json.dumps(arg)
        resp = requests.post(
            CONTENT + "/files/download",
            headers=headers,
            stream=True,
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # Dropbox /files/download streams chunked (no Content-Length); the real
            # size is in the Dropbox-API-Result header metadata. Fall back to
            # Content-Length if present.
            total = 0
            try:
                total = int(
                    json.loads(resp.headers.get("Dropbox-API-Result", "{}")).get(
                        "size", 0
                    )
                )
            except Exception:
                total = 0
            if not total:
                try:
                    total = int(resp.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    total = 0
            written = 0
            cancelled = False
            try:
                with open(dest, "wb") as fh:
                    for block in resp.iter_content(chunk_size=1024 * 1024):
                        if block:
                            fh.write(block)
                            written += len(block)
                            if progress is not None:
                                # A buggy callback must never corrupt the stream, so its
                                # OWN errors are swallowed; but an explicit False is a
                                # cancel request and is honored. `is False` (not falsy) so
                                # a callback that just updates and returns None never
                                # cancels.
                                cont = True
                                try:
                                    cont = progress(written, total)
                                except Exception:
                                    cont = True
                                if cont is False:
                                    cancelled = True
                                    break
            except Exception:
                # A broken stream leaves a partial temp file: remove it so a
                # later restore can never pick up a truncated zip, then re-raise.
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise
            if cancelled:
                # User canceled mid-download: drop the partial (never leave a truncated
                # zip for a later restore) and signal the caller. Raised OUTSIDE the
                # try/except above so the cancel is never swallowed as a stream error.
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise DropboxCanceled()
            _log("download wrote %s bytes for %s" % (written, remote_name))
            # A 200 with no body would otherwise become a silent "restore complete"
            # on an empty file - fail loud instead so the caller surfaces it.
            if written == 0:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise DropboxAuthError("download produced an empty file (0 bytes)")
            return ret
        if attempt == 1 and _handle_retryable(resp):
            continue
        raise DropboxAuthError("download failed (HTTP %s)" % resp.status_code)


# --------------------------------------------------------------------------- delete


def delete(remote_name):
    arg = {"path": "/" + remote_name}
    resp = _rpc(API + "/files/delete_v2", arg)
    if _handle_retryable(resp):
        resp = _rpc(API + "/files/delete_v2", arg, force=True)
    if resp.status_code != 200:
        raise DropboxAuthError("delete failed (HTTP %s)" % resp.status_code)
    return True
