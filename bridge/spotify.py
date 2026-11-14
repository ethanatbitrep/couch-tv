"""Spotify for the TV home: what you played lately, what is playing now, and playing a song on the TV.

    "spotify": {
      "client_id": "the Client ID of your own Spotify app (developer.spotify.com/dashboard)",
      "device_name": "optional: part of the TV's name in Spotify's device list, for example Living Room"
    }

Setup, in plain words: docs/SPOTIFY.md. Off unless `client_id` is set.

Sign-in is Spotify's Authorization Code flow with PKCE (no client secret): someone at the bridge
computer opens http://127.0.0.1:<port>/spotify/login once (only the computer itself may, see
server.py), approves, and Spotify sends them back to /spotify/callback, the Redirect URI the
Spotify app must list exactly. The tokens are kept in bridge/data/spotify.json (mode 0600, git
ignores bridge/data/) and refreshed when they run out or Spotify answers 401.

Playing on the TV uses Spotify Connect: the TV's Spotify app only shows up in /me/player/devices
while it is open, so when it is not listed the bridge opens Spotify on the TV (the caller passes how,
server.py uses the TV's own launch_package) and waits up to POLL_FOR seconds for it to appear.

Endpoints used (all still open to Development Mode apps after Spotify's February 2026 changes):
POST accounts.spotify.com/api/token, GET /me/player/recently-played, GET /me/player,
GET /me/player/devices, PUT /me/player/play, PUT /me/player/pause, POST /me/player/next,
POST /me/player/previous and PUT /me/player (transfer). If Spotify ever answers 403, 404 or 410 for
a read, the card shows less instead of failing, and the log says so once.
"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import time
import urllib.parse

import config

log = logging.getLogger("spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-recently-played user-read-playback-state user-modify-playback-state user-read-currently-playing"

_cfg = config.CONFIG.get("spotify") or {}
CLIENT_ID = str(_cfg.get("client_id") or "").strip()
DEVICE_NAME = str(_cfg.get("device_name") or "").strip()
ENABLED = bool(CLIENT_ID)
PORT = int(os.environ.get("PORT") or config.BRIDGE["port"])
# Spotify only takes loopback redirects as an explicit IP address, never "localhost".
REDIRECT_URI = f"http://127.0.0.1:{PORT}/spotify/callback"
TOKEN_FILE = os.path.join(config.DATA, "spotify.json")

RECENT_MAX = 8          # songs in the "recently played" row
RECENT_EVERY = 30       # seconds a recently played answer is reused
NOW_EVERY = 3           # seconds a now playing answer is reused (several screens may ask)
POLL_EVERY = 1.5        # seconds between looks for the TV in Spotify's device list
POLL_FOR = 20           # seconds to wait for the TV's Spotify to show up after opening it
LOGIN_TTL = 600         # seconds a sign-in may take, from /spotify/login to /spotify/callback
ART_SIZE = 300          # the cover size the home wants (Spotify has 640, 300 and 64)

ACTIONS = {"play": ("PUT", "/me/player/play"), "pause": ("PUT", "/me/player/pause"),
           "next": ("POST", "/me/player/next"), "previous": ("POST", "/me/player/previous")}
OFFSET_CONTEXTS = ("album", "playlist")   # the only contexts Spotify can start at a chosen song

_pending = {}           # state -> (code verifier, created at), for sign-ins in progress
_cache = {"recent": (0.0, None), "now": (0.0, None)}
_said = set()           # (path, status) already logged, so a missing endpoint is said once
_token_lock = asyncio.Lock()


class NotConnected(Exception):
    """Spotify is not set up or not signed in; the message says what to do, in plain words."""


class LoginError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- the network (the one place that talks to Spotify; the tests replace it) ----------
async def _http(method, url, *, headers=None, form=None, json_body=None, timeout=10):
    """-> (status, parsed JSON body or None, headers)."""
    import aiohttp
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form)
        headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
    elif json_body is None and method in ("PUT", "POST"):
        data = b""   # Spotify wants a Content-Length on a PUT or POST without a body
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request(method, url, headers=headers, data=data, json=json_body) as r:
            text = await r.text()
            try:
                parsed = json.loads(text) if text.strip() else None
            except ValueError:
                parsed = None
            return r.status, parsed, dict(r.headers)


def _say_once(path, status, what):
    if (path, status) not in _said:
        _said.add((path, status))
        log.warning("Spotify %s answered %s: %s (the Spotify card shows less until this changes)", path, status, what)


def _error_text(data, fallback):
    err = (data or {}).get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or fallback)
    if isinstance(err, str):
        return str((data or {}).get("error_description") or err)
    return fallback


# ---------- tokens ----------
def _load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        return t if isinstance(t, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_tokens(tokens):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    tmp = f"{TOKEN_FILE}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)
    os.chmod(TOKEN_FILE, 0o600)


def _forget_tokens():
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def connected():
    """True when signed in with the client id in config.json (a changed client id means signing in again)."""
    t = _load_tokens()
    return ENABLED and bool(t.get("refresh_token")) and t.get("client_id") == CLIENT_ID


def _keep(answer, old_refresh=None):
    """Spotify's token answer -> what is saved (a PKCE refresh may hand out a new refresh token)."""
    return {"client_id": CLIENT_ID, "access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token") or old_refresh,
            "expires_at": time.time() + int(answer.get("expires_in") or 3600) - 60,
            "scope": answer.get("scope") or ""}


async def _access_token(force_refresh=False):
    if not ENABLED:
        raise NotConnected("Spotify is not set up: add spotify.client_id to config.json (docs/SPOTIFY.md).")
    async with _token_lock:
        t = _load_tokens()
        if not t.get("refresh_token") or t.get("client_id") != CLIENT_ID:
            raise NotConnected(f"Spotify is not signed in yet: on the bridge computer open "
                               f"http://127.0.0.1:{PORT}/spotify/login (docs/SPOTIFY.md).")
        if not force_refresh and t.get("access_token") and time.time() < float(t.get("expires_at") or 0):
            return t["access_token"]
        status, data, _ = await _http("POST", TOKEN_URL, form={
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": CLIENT_ID})
        if status == 200 and isinstance(data, dict) and data.get("access_token"):
            t = _keep(data, t["refresh_token"])
            _save_tokens(t)
            return t["access_token"]
        if status in (400, 401) and isinstance(data, dict) and data.get("error") == "invalid_grant":
            log.warning("Spotify refused the saved sign-in (%s); sign in again at /spotify/login",
                        _error_text(data, "invalid_grant"))
            _forget_tokens()
            raise NotConnected(f"Spotify's sign-in ran out: on the bridge computer open "
                               f"http://127.0.0.1:{PORT}/spotify/login again.")
        raise RuntimeError(f"Spotify token refresh failed: {status} {_error_text(data, '')}".strip())


async def _api(method, path, *, params=None, body=None):
    """One Web API call, signed in; refreshes and retries once on 401. -> (status, JSON or None)."""
    url = API + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    for attempt in (0, 1):
        token = await _access_token(force_refresh=attempt == 1)
        status, data, headers = await _http(method, url, headers={"Authorization": f"Bearer {token}"},
                                            json_body=body)
        if status != 401:
            break
    if status == 429:
        _say_once(path, 429, f"too many requests, retry after {(headers or {}).get('Retry-After', '?')} s")
    return status, data


# ---------- sign-in (Authorization Code with PKCE) ----------
def is_loopback(remote):
    ip = str(remote or "").removeprefix("::ffff:")
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def start_login(remote):
    """The Spotify address to send the browser to. Only for the bridge computer itself."""
    if not is_loopback(remote):
        raise LoginError("Spotify sign-in only works on the bridge computer itself: open "
                         f"http://127.0.0.1:{PORT}/spotify/login there.", 403)
    if not ENABLED:
        raise LoginError("Spotify is not set up: add spotify.client_id to config.json and restart the bridge "
                         "(docs/SPOTIFY.md).", 404)
    now = time.time()
    for s, (_, at) in list(_pending.items()):
        if now - at > LOGIN_TTL:
            _pending.pop(s, None)
    while len(_pending) >= 5:   # a few tabs at most
        _pending.pop(next(iter(_pending)))
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)   # 86 characters, inside PKCE's 43 to 128
    _pending[state] = (verifier, now)
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": _challenge(verifier),
        "state": state, "scope": SCOPES})


async def finish_login(remote, query):
    """Spotify sent the browser back with ?code=&state= (or ?error=). Saves the tokens."""
    if not is_loopback(remote):
        raise LoginError("Spotify sign-in only works on the bridge computer itself.", 403)
    if not ENABLED:
        raise LoginError("Spotify is not set up: add spotify.client_id to config.json.", 404)
    state = str(query.get("state") or "")
    got = _pending.pop(state, None) if state else None
    if not got or time.time() - got[1] > LOGIN_TTL:
        raise LoginError("This sign-in link is old or was already used. "
                         f"Start again at http://127.0.0.1:{PORT}/spotify/login.")
    if query.get("error"):
        raise LoginError(f"Spotify did not connect: {query.get('error')}. "
                         f"Start again at http://127.0.0.1:{PORT}/spotify/login.")
    code = str(query.get("code") or "")
    if not code:
        raise LoginError("Spotify sent no sign-in code. Start again at /spotify/login.")
    status, data, _ = await _http("POST", TOKEN_URL, form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "code_verifier": got[0]})
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        log.warning("Spotify sign-in failed: %s %s", status, _error_text(data, ""))
        raise LoginError(f"Spotify said no ({_error_text(data, status)}). Check the Redirect URI in your "
                         f"Spotify app is exactly {REDIRECT_URI}, then start again.", 502)
    _save_tokens(_keep(data))
    _cache["recent"] = _cache["now"] = (0.0, None)
    log.info("Spotify connected")


# ---------- what to show ----------
def _art(images, size=ART_SIZE):
    """The cover closest to `size` pixels wide (Spotify lists 640, 300 and 64)."""
    images = [i for i in images or [] if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    return min(images, key=lambda i: abs((i.get("width") or size) - size))["url"]


def _artists(item):
    if item.get("type") == "episode":
        return str((item.get("show") or {}).get("name") or "")
    return ", ".join(str(a.get("name")) for a in item.get("artists") or [] if a.get("name"))


def _images(item):
    if item.get("type") == "episode":
        return item.get("images") or (item.get("show") or {}).get("images")
    return (item.get("album") or {}).get("images")


def shape_recent(items, limit=RECENT_MAX):
    """recently-played items -> [{name, artists, album, art, uri, context_uri}], newest first, each song once."""
    out, seen = [], set()
    for it in items or []:
        track = (it or {}).get("track") or {}
        uri = track.get("uri")
        if not uri or uri in seen or track.get("is_local"):
            continue
        seen.add(uri)
        out.append({"name": track.get("name") or "", "artists": _artists(track),
                    "album": (track.get("album") or {}).get("name") or "",
                    "art": _art(_images(track)), "uri": uri,
                    "context_uri": ((it.get("context") or {}).get("uri")) or None})
        if len(out) >= limit:
            break
    return out


async def recent():
    """The last songs played on the account (any device), up to 8, each once. [] when unknown."""
    at, got = _cache["recent"]
    if got is not None and time.time() - at < RECENT_EVERY:
        return got
    path = "/me/player/recently-played"
    try:
        status, data = await _api("GET", path, params={"limit": 50})
    except NotConnected:
        raise
    except Exception as e:
        log.warning("Spotify recently played: %s", e)
        return got or []
    if status != 200:
        if status in (403, 404, 410):
            _say_once(path, status, _error_text(data, "not available to this app"))
        elif status != 429:
            log.warning("Spotify recently played answered %s", status)
        return got or []
    got = shape_recent((data or {}).get("items"))
    _cache["recent"] = (time.time(), got)
    return got


def shape_now(state):
    item = (state or {}).get("item")
    if not item:
        return None
    dev = state.get("device") or {}
    return {"name": item.get("name") or "", "artists": _artists(item), "art": _art(_images(item)),
            "is_playing": bool(state.get("is_playing")), "progress_ms": state.get("progress_ms"),
            "duration_ms": item.get("duration_ms"), "uri": item.get("uri"),
            "device": {"name": dev.get("name"), "type": dev.get("type")} if dev else None}


async def now_playing():
    """What Spotify is playing now on any device, or None."""
    at, got = _cache["now"]
    if time.time() - at < NOW_EVERY:
        return got
    path = "/me/player"
    try:
        status, data = await _api("GET", path, params={"additional_types": "track,episode"})
    except NotConnected:
        raise
    except Exception as e:
        log.warning("Spotify now playing: %s", e)
        return got
    if status == 204:
        got = None
    elif status == 200:
        got = shape_now(data)
    else:
        if status in (403, 404, 410):
            _say_once(path, status, _error_text(data, "not available to this app"))
        return got
    _cache["now"] = (time.time(), got)
    return got


async def summary():
    """For GET /api/spotify: {connected, recent, now} or {connected: false, reason}."""
    if not ENABLED:
        return {"connected": False, "reason": "Spotify is not set up (docs/SPOTIFY.md).", "recent": [], "now": None}
    try:
        r, n = await asyncio.gather(recent(), now_playing())
    except NotConnected as e:
        return {"connected": False, "reason": str(e), "recent": [], "now": None}
    return {"connected": True, "recent": r, "now": n}


# ---------- playing on the TV ----------
def pick_device(devices, wanted=None):
    """The TV among Spotify Connect devices: the configured name if set, else a TV or a Samsung."""
    wanted = DEVICE_NAME if wanted is None else wanted
    usable = [d for d in devices or [] if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]
    if wanted:
        found = [d for d in usable if wanted.lower() in str(d.get("name") or "").lower()]
    else:
        found = [d for d in usable if str(d.get("type") or "").lower() == "tv"
                 or "samsung" in str(d.get("name") or "").lower()]
    found.sort(key=lambda d: not d.get("is_active"))   # an active one first
    return found[0] if found else None


async def _devices():
    status, data = await _api("GET", "/me/player/devices")
    if status != 200:
        if status in (403, 404, 410):
            _say_once("/me/player/devices", status, _error_text(data, "not available to this app"))
        return []
    return (data or {}).get("devices") or []


def _valid(uri, kinds):
    parts = str(uri or "").split(":")
    return len(parts) == 3 and parts[0] == "spotify" and parts[1] in kinds and parts[2].isalnum()


def play_body(uri=None, context_uri=None):
    """The body for PUT /me/player/play. A song from an album or playlist starts there, in its
    context; from an artist or show (no offset allowed) the song plays on its own."""
    if context_uri and context_uri.split(":")[1] in OFFSET_CONTEXTS:
        return {"context_uri": context_uri, **({"offset": {"uri": uri}} if uri else {})}
    if uri:
        return {"uris": [uri]}
    if context_uri:
        return {"context_uri": context_uri}
    return None   # carry on with whatever was playing


async def find_tv(launch=None):
    """-> (device or None, launched). Opens Spotify on the TV with `launch` when it is not listed."""
    dev = pick_device(await _devices())
    if dev or launch is None:
        return dev, False
    try:
        await launch()
    except Exception as e:
        log.info("could not open Spotify on the TV: %s", e)
        raise RuntimeError(f"Could not open Spotify on the TV ({e}).")
    deadline = time.monotonic() + POLL_FOR
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_EVERY)
        dev = pick_device(await _devices())
        if dev:
            return dev, True
    return None, True


async def play(uri=None, context_uri=None, launch=None):
    """Play a song (uri), an album or playlist (context_uri, starting at uri if given), or carry on,
    on the TV. `launch` is an async function that opens Spotify on the TV.
    -> {"ok": True, "device": name, "launched": bool} or {"ok": False, "error": plain words}."""
    if uri and not _valid(uri, ("track", "episode")):
        return {"ok": False, "error": "uri must be a Spotify song or episode (spotify:track:...)"}
    if context_uri and not _valid(context_uri, ("album", "playlist", "artist", "show")):
        return {"ok": False, "error": "context_uri must be a Spotify album, playlist, artist or show"}
    try:
        dev, launched = await find_tv(launch)
        if not dev:
            name = f'"{DEVICE_NAME}"' if DEVICE_NAME else "the TV"
            return {"ok": False, "launched": launched, "error": (
                f"Spotify did not show up on {name}. Open Spotify on the TV once, check it is signed in "
                "to the same Spotify account, then try again.")}
        body = play_body(uri, context_uri)
        params = {"device_id": dev["id"]}
        status, data = await _api("PUT", "/me/player/play", params=params, body=body)
        if status == 404:
            # listed but asleep: hand playback to it first, then ask again
            await _api("PUT", "/me/player", body={"device_ids": [dev["id"]], "play": False})
            await asyncio.sleep(1)
            status, data = await _api("PUT", "/me/player/play", params=params, body=body)
        _cache["now"] = (0.0, None)
        if status in (200, 202, 204):
            return {"ok": True, "device": dev.get("name"), "launched": launched}
        if status == 403:
            return {"ok": False, "error": f"Spotify would not play that: {_error_text(data, 'not allowed')} "
                    "(Spotify Premium is needed to play from the bridge)."}
        return {"ok": False, "error": f"Spotify did not start playing ({status}: {_error_text(data, 'no reason given')})."}
    except NotConnected as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.warning("Spotify play: %s", e)
        return {"ok": False, "error": str(e) if str(e).startswith("Could not") else f"Could not reach Spotify ({e})."}


async def control(action, launch=None):
    """play, pause, next or previous on the device Spotify is playing on.
    -> {"ok": True} or {"ok": False, "error": plain words}."""
    if action not in ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(ACTIONS)}"}
    method, path = ACTIONS[action]
    try:
        status, data = await _api(method, path)
    except NotConnected as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.warning("Spotify %s: %s", action, e)
        return {"ok": False, "error": f"Could not reach Spotify ({e})."}
    _cache["now"] = (0.0, None)
    if status in (200, 202, 204):
        return {"ok": True}
    if status == 404:
        if action == "play":   # nothing active: carry on on the TV, opening Spotify there if needed
            return await play(launch=launch)
        return {"ok": False, "error": "Nothing is playing on Spotify right now."}
    if status == 403:
        return {"ok": False, "error": f"Spotify would not do that: {_error_text(data, 'not allowed')}."}
    return {"ok": False, "error": f"Spotify answered {status}: {_error_text(data, 'no reason given')}."}
