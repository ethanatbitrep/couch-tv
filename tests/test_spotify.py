"""bridge/spotify.py: PKCE sign-in, token refresh, recently played, picking the TV, playing on it.

Spotify is never reached: spotify._http is replaced by a fake that answers from a script.
Run: python3 tests/test_spotify.py   (or python3 -m unittest tests.test_spotify)
"""
import asyncio
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))
os.environ.setdefault("COUCH_DATA", tempfile.mkdtemp(prefix="couch-spotify-"))
import spotify  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeSpotify:
    """Stands in for spotify._http. `routes` maps (method, path) -> list of answers, used in order
    (the last one repeats). An answer is (status, body) or a function(call) -> (status, body)."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    async def __call__(self, method, url, *, headers=None, form=None, json_body=None, timeout=10):
        u = urllib.parse.urlsplit(url)
        path = u.path.removeprefix("/v1") if u.netloc == "api.spotify.com" else u.path
        call = {"method": method, "path": path, "query": dict(urllib.parse.parse_qsl(u.query)),
                "headers": headers or {}, "form": form, "json": json_body}
        self.calls.append(call)
        answers = self.routes.get((method, path))
        if not answers:
            return 404, {"error": {"status": 404, "message": "no route in the fake"}}, {}
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        status, data = a(call) if callable(a) else a
        return status, data, {}

    def count(self, method, path):
        return sum(1 for c in self.calls if c["method"] == method and c["path"] == path)


def track(n, album_images=None):
    return {"name": f"Song {n}", "uri": f"spotify:track:t{n}", "type": "track", "duration_ms": 200000,
            "artists": [{"name": "Artist A"}, {"name": "Artist B"}],
            "album": {"name": f"Album {n}", "images": album_images if album_images is not None else [
                {"url": f"https://i/{n}/640", "width": 640, "height": 640},
                {"url": f"https://i/{n}/300", "width": 300, "height": 300},
                {"url": f"https://i/{n}/64", "width": 64, "height": 64}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="couch-spotify-")
        self.saved = {k: getattr(spotify, k) for k in
                      ("CLIENT_ID", "ENABLED", "DEVICE_NAME", "TOKEN_FILE", "POLL_EVERY", "POLL_FOR", "_http")}
        spotify.CLIENT_ID, spotify.ENABLED, spotify.DEVICE_NAME = "abc123client", True, ""
        spotify.TOKEN_FILE = os.path.join(self.dir, "spotify.json")
        spotify.POLL_EVERY, spotify.POLL_FOR = 0.01, 0.2
        spotify._pending.clear()
        spotify._said.clear()
        spotify._cache.update(recent=(0.0, None), now=(0.0, None))
        spotify._token_lock = asyncio.Lock()
        self._sleep = spotify.asyncio.sleep

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(spotify, k, v)

    def sign_in(self, expires_in=3600, access="old-access"):
        spotify._save_tokens({"client_id": spotify.CLIENT_ID, "access_token": access,
                              "refresh_token": "refresh-1", "expires_at": time.time() + expires_in})

    def fake(self, routes):
        f = FakeSpotify(routes)
        spotify._http = f
        return f


class TestLogin(Base):
    def test_pkce_url(self):
        url = spotify.start_login("127.0.0.1")
        u = urllib.parse.urlsplit(url)
        self.assertEqual(f"{u.scheme}://{u.netloc}{u.path}", spotify.AUTH_URL)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.assertEqual(q["client_id"], "abc123client")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], f"http://127.0.0.1:{spotify.PORT}/spotify/callback")
        self.assertNotIn("localhost", q["redirect_uri"])
        self.assertEqual(set(q["scope"].split()), {"user-read-recently-played", "user-read-playback-state",
                                                   "user-modify-playback-state", "user-read-currently-playing"})
        verifier, _ = spotify._pending[q["state"]]
        self.assertTrue(43 <= len(verifier) <= 128)
        want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(q["code_challenge"], want)
        self.assertNotIn("=", q["code_challenge"])

    def test_login_is_loopback_only(self):
        for remote in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(spotify.start_login(remote).startswith(spotify.AUTH_URL))
        for remote in ("192.0.2.20", "198.51.100.5", "", None, "not-an-ip"):
            with self.assertRaises(spotify.LoginError) as e:
                spotify.start_login(remote)
            self.assertEqual(e.exception.status, 403)
        with self.assertRaises(spotify.LoginError) as e:
            run(spotify.finish_login("192.0.2.20", {"code": "c", "state": "s"}))
        self.assertEqual(e.exception.status, 403)

    def test_not_set_up(self):
        spotify.ENABLED, spotify.CLIENT_ID = False, ""
        with self.assertRaises(spotify.LoginError) as e:
            spotify.start_login("127.0.0.1")
        self.assertEqual(e.exception.status, 404)
        got = run(spotify.summary())
        self.assertFalse(got["connected"])
        self.assertIn("not set up", got["reason"])

    def test_callback_swaps_code_and_saves_0600(self):
        f = self.fake({("POST", "/api/token"): [(200, {"access_token": "A1", "refresh_token": "R1",
                                                       "expires_in": 3600, "scope": spotify.SCOPES})]})
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(spotify.start_login("127.0.0.1")).query))["state"]
        verifier = spotify._pending[state][0]
        run(spotify.finish_login("127.0.0.1", {"code": "the-code", "state": state}))
        form = f.calls[0]["form"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code_verifier"], verifier)
        self.assertEqual(form["client_id"], "abc123client")
        self.assertNotIn("client_secret", form)
        self.assertEqual(stat.S_IMODE(os.stat(spotify.TOKEN_FILE).st_mode), 0o600)
        with open(spotify.TOKEN_FILE) as fh:
            saved = json.load(fh)
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("A1", "R1"))
        self.assertTrue(spotify.connected())
        # the state is single use
        with self.assertRaises(spotify.LoginError):
            run(spotify.finish_login("127.0.0.1", {"code": "the-code", "state": state}))

    def test_callback_bad_state_or_denied(self):
        self.fake({})
        with self.assertRaises(spotify.LoginError):
            run(spotify.finish_login("127.0.0.1", {"code": "x", "state": "made-up"}))
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(spotify.start_login("127.0.0.1")).query))["state"]
        with self.assertRaises(spotify.LoginError) as e:
            run(spotify.finish_login("127.0.0.1", {"error": "access_denied", "state": state}))
        self.assertIn("access_denied", str(e.exception))
        self.assertFalse(spotify.connected())

    def test_changed_client_id_means_not_connected(self):
        self.sign_in()
        self.assertTrue(spotify.connected())
        spotify.CLIENT_ID = "another"
        self.assertFalse(spotify.connected())


class TestTokens(Base):
    def test_refresh_on_401(self):
        self.sign_in()
        f = self.fake({
            ("GET", "/me/player/recently-played"): [
                lambda c: (401, {"error": {"status": 401, "message": "expired"}})
                if c["headers"]["Authorization"] == "Bearer old-access" else (200, {"items": [{"track": track(1)}]})],
            ("POST", "/api/token"): [(200, {"access_token": "new-access", "expires_in": 3600})]})
        got = run(spotify.recent())
        self.assertEqual([r["uri"] for r in got], ["spotify:track:t1"])
        self.assertEqual(f.count("POST", "/api/token"), 1)
        self.assertEqual(f.calls[1]["form"]["grant_type"], "refresh_token")
        self.assertEqual(f.calls[1]["form"]["refresh_token"], "refresh-1")
        with open(spotify.TOKEN_FILE) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(saved["refresh_token"], "refresh-1")   # kept when Spotify sends no new one

    def test_refresh_when_expired_and_rotated_refresh_token(self):
        self.sign_in(expires_in=-10)
        f = self.fake({("POST", "/api/token"): [(200, {"access_token": "A2", "refresh_token": "refresh-2", "expires_in": 3600})],
                       ("GET", "/me/player"): [(204, None)]})
        self.assertIsNone(run(spotify.now_playing()))
        self.assertEqual(f.calls[0]["path"], "/api/token")
        self.assertEqual(f.calls[1]["headers"]["Authorization"], "Bearer A2")
        with open(spotify.TOKEN_FILE) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "refresh-2")

    def test_revoked_refresh_token(self):
        self.sign_in(expires_in=-10)
        self.fake({("POST", "/api/token"): [(400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})]})
        got = run(spotify.summary())
        self.assertFalse(got["connected"])
        self.assertIn("/spotify/login", got["reason"])
        self.assertFalse(os.path.exists(spotify.TOKEN_FILE))


class TestRecentAndNow(Base):
    def test_recent_dedupes_and_limits(self):
        self.sign_in()
        items = [{"track": track(1), "context": {"uri": "spotify:album:al1"}}, {"track": track(1)},
                 {"track": track(2), "context": None}, {"track": track(1)}]
        items += [{"track": track(n)} for n in range(3, 15)]
        items.append({"track": {**track(99), "is_local": True}})
        f = self.fake({("GET", "/me/player/recently-played"): [(200, {"items": items})]})
        got = run(spotify.recent())
        self.assertEqual(len(got), 8)
        self.assertEqual([g["uri"] for g in got], [f"spotify:track:t{n}" for n in (1, 2, 3, 4, 5, 6, 7, 8)])
        first = got[0]
        self.assertEqual(first["artists"], "Artist A, Artist B")
        self.assertEqual(first["album"], "Album 1")
        self.assertEqual(first["art"], "https://i/1/300")
        self.assertEqual(first["context_uri"], "spotify:album:al1")
        self.assertIsNone(got[1]["context_uri"])
        self.assertEqual(f.calls[0]["query"]["limit"], "50")
        run(spotify.recent())   # cached for 30 s
        self.assertEqual(f.count("GET", "/me/player/recently-played"), 1)

    def test_art_choice(self):
        self.assertEqual(spotify._art([{"url": "a", "width": 640}, {"url": "b", "width": 300}]), "b")
        self.assertEqual(spotify._art([{"url": "a", "width": 640}]), "a")
        self.assertEqual(spotify._art([{"url": "c", "width": None}]), "c")
        self.assertIsNone(spotify._art([]))

    def test_removed_endpoint_degrades(self):
        self.sign_in()
        self.fake({("GET", "/me/player/recently-played"): [(403, {"error": {"status": 403, "message": "gone"}})],
                   ("GET", "/me/player"): [(410, None)]})
        with self.assertLogs("spotify", "WARNING") as logs:
            got = run(spotify.summary())
            spotify._cache.update(recent=(0.0, None), now=(0.0, None))
            run(spotify.summary())
        self.assertTrue(got["connected"])
        self.assertEqual(got["recent"], [])
        self.assertIsNone(got["now"])
        self.assertEqual(len(logs.output), 2)   # one line per endpoint, not one per request

    def test_now_playing(self):
        self.sign_in()
        state = {"is_playing": True, "progress_ms": 1234, "item": track(5),
                 "device": {"id": "d1", "name": "Living Room TV", "type": "TV"}}
        f = self.fake({("GET", "/me/player"): [(200, state)]})
        now = run(spotify.now_playing())
        self.assertEqual(now["name"], "Song 5")
        self.assertEqual(now["artists"], "Artist A, Artist B")
        self.assertEqual(now["art"], "https://i/5/300")
        self.assertTrue(now["is_playing"])
        self.assertEqual((now["progress_ms"], now["duration_ms"]), (1234, 200000))
        self.assertEqual(now["device"], {"name": "Living Room TV", "type": "TV"})
        self.assertIn("episode", f.calls[0]["query"]["additional_types"])


class TestDevices(Base):
    DEVICES = [{"id": "p1", "name": "Phone", "type": "Smartphone", "is_active": True},
               {"id": "s1", "name": "Speaker", "type": "Speaker"},
               {"id": "t1", "name": "[TV] Samsung 7 Series (55)", "type": "TV"},
               {"id": "r1", "name": "Restricted TV", "type": "TV", "is_restricted": True}]

    def test_default_picks_tv(self):
        self.assertEqual(spotify.pick_device(self.DEVICES, "")["id"], "t1")
        self.assertEqual(spotify.pick_device([{"id": "x", "name": "Samsung soundbar", "type": "Speaker"}], "")["id"], "x")
        self.assertIsNone(spotify.pick_device(self.DEVICES[:2], ""))
        self.assertIsNone(spotify.pick_device([self.DEVICES[3]], ""))   # restricted: cannot be driven

    def test_configured_name(self):
        self.assertEqual(spotify.pick_device(self.DEVICES, "speaker")["id"], "s1")
        self.assertIsNone(spotify.pick_device(self.DEVICES, "bedroom"))

    def test_prefers_active(self):
        devs = [{"id": "a", "name": "TV one", "type": "TV"}, {"id": "b", "name": "TV two", "type": "TV", "is_active": True}]
        self.assertEqual(spotify.pick_device(devs, "")["id"], "b")


class TestSpeaker(Base):
    """spotify.speaker: play on an Echo or another speaker, never opening Spotify on the TV."""
    ECHO = {"id": "e1", "name": "Ethan's Echo Dot", "type": "Speaker"}

    def setUp(self):
        super().setUp()
        self._was = spotify.SPEAKER
        spotify.SPEAKER = "echo"

    def tearDown(self):
        spotify.SPEAKER = self._was
        super().tearDown()

    def test_plays_on_speaker_not_tv(self):
        self.sign_in()
        f = self.fake({("GET", "/me/player/devices"): [(200, {"devices": TestDevices.DEVICES + [self.ECHO]})],
                       ("PUT", "/me/player/play"): [(204, None)]})
        launched = []

        async def launch():
            launched.append(1)
        got = run(spotify.play(uri="spotify:track:t1", launch=launch))
        self.assertEqual(got, {"ok": True, "device": "Ethan's Echo Dot", "launched": False})
        self.assertEqual(launched, [])
        put = [c for c in f.calls if c["path"] == "/me/player/play"][0]
        self.assertEqual(put["query"], {"device_id": "e1"})

    def test_speaker_missing_never_opens_tv(self):
        self.sign_in()
        self.fake({("GET", "/me/player/devices"): [(200, {"devices": TestDevices.DEVICES})]})
        launched = []

        async def launch():
            launched.append(1)
        got = run(spotify.play(uri="spotify:track:t1", launch=launch))
        self.assertFalse(got["ok"])
        self.assertIn("Alexa", got["error"])
        self.assertEqual(launched, [])

    def test_device_list(self):
        self.sign_in()
        self.fake({("GET", "/me/player/devices"): [(200, {"devices": [TestDevices.DEVICES[0], self.ECHO]})]})
        got = run(spotify.devices())
        self.assertEqual(got["speaker"], "echo")
        self.assertEqual([d["name"] for d in got["devices"]], ["Phone", "Ethan's Echo Dot"])


class TestPlay(Base):
    def test_play_when_tv_listed(self):
        self.sign_in()
        f = self.fake({("GET", "/me/player/devices"): [(200, {"devices": TestDevices.DEVICES})],
                       ("PUT", "/me/player/play"): [(204, None)]})
        launched = []

        async def launch():
            launched.append(1)
        got = run(spotify.play(uri="spotify:track:t1", context_uri="spotify:album:al1", launch=launch))
        self.assertEqual(got, {"ok": True, "device": "[TV] Samsung 7 Series (55)", "launched": False})
        self.assertEqual(launched, [])
        put = [c for c in f.calls if c["path"] == "/me/player/play"][0]
        self.assertEqual(put["query"], {"device_id": "t1"})
        self.assertEqual(put["json"], {"context_uri": "spotify:album:al1", "offset": {"uri": "spotify:track:t1"}})

    def test_play_bodies(self):
        self.assertEqual(spotify.play_body("spotify:track:a"), {"uris": ["spotify:track:a"]})
        self.assertEqual(spotify.play_body("spotify:track:a", "spotify:artist:b"), {"uris": ["spotify:track:a"]})
        self.assertEqual(spotify.play_body(None, "spotify:playlist:p"), {"context_uri": "spotify:playlist:p"})
        self.assertIsNone(spotify.play_body())

    def test_launch_then_poll(self):
        self.sign_in()
        tv = {"id": "t1", "name": "Samsung 7 Series", "type": "TV"}
        launched = []

        def devices(call):
            # empty until Spotify was opened on the TV, then empty twice more, then the TV
            if not launched or f.count("GET", "/me/player/devices") < 4:
                return 200, {"devices": [TestDevices.DEVICES[0]]}
            return 200, {"devices": [TestDevices.DEVICES[0], tv]}
        f = self.fake({("GET", "/me/player/devices"): [devices], ("PUT", "/me/player/play"): [(204, None)]})

        async def launch():
            launched.append(1)
        got = run(spotify.play(uri="spotify:track:t2", launch=launch))
        self.assertTrue(got["ok"], got)
        self.assertTrue(got["launched"])
        self.assertEqual(launched, [1])
        self.assertEqual(f.count("GET", "/me/player/devices"), 4)
        put = [c for c in f.calls if c["path"] == "/me/player/play"][0]
        self.assertEqual(put["json"], {"uris": ["spotify:track:t2"]})

    def test_tv_never_shows_up(self):
        self.sign_in()
        self.fake({("GET", "/me/player/devices"): [(200, {"devices": []})]})

        async def launch():
            pass
        got = run(spotify.play(uri="spotify:track:t2", launch=launch))
        self.assertFalse(got["ok"])
        self.assertIn("did not show up", got["error"])

    def test_launch_fails(self):
        self.sign_in()
        self.fake({("GET", "/me/player/devices"): [(200, {"devices": []})]})

        async def launch():
            raise RuntimeError("the TV is not answering on the network")
        got = run(spotify.play(uri="spotify:track:t2", launch=launch))
        self.assertFalse(got["ok"])
        self.assertIn("Could not open Spotify on the TV", got["error"])

    def test_play_404_transfers_then_retries(self):
        self.sign_in()
        f = self.fake({("GET", "/me/player/devices"): [(200, {"devices": TestDevices.DEVICES})],
                       ("PUT", "/me/player/play"): [(404, {"error": {"status": 404, "message": "Device not found"}}), (204, None)],
                       ("PUT", "/me/player"): [(204, None)]})
        spotify.asyncio.sleep = lambda s: self._sleep(0)
        try:
            got = run(spotify.play(uri="spotify:track:t1"))
        finally:
            spotify.asyncio.sleep = self._sleep
        self.assertTrue(got["ok"], got)
        transfer = [c for c in f.calls if c["path"] == "/me/player"][0]
        self.assertEqual(transfer["json"], {"device_ids": ["t1"], "play": False})
        self.assertEqual(f.count("PUT", "/me/player/play"), 2)

    def test_bad_uris_refused(self):
        self.sign_in()
        self.fake({})
        self.assertFalse(run(spotify.play(uri="https://evil/x"))["ok"])
        self.assertFalse(run(spotify.play(uri="spotify:track:a/../b"))["ok"])
        self.assertFalse(run(spotify.play(context_uri="spotify:user:x"))["ok"])

    def test_not_signed_in(self):
        self.fake({})
        got = run(spotify.play(uri="spotify:track:t1"))
        self.assertFalse(got["ok"])
        self.assertIn("/spotify/login", got["error"])


class TestControl(Base):
    def test_actions(self):
        self.sign_in()
        f = self.fake({("PUT", "/me/player/pause"): [(204, None)], ("POST", "/me/player/next"): [(204, None)],
                       ("POST", "/me/player/previous"): [(204, None)], ("PUT", "/me/player/play"): [(204, None)]})
        for a in ("pause", "next", "previous", "play"):
            self.assertEqual(run(spotify.control(a)), {"ok": True}, a)
        self.assertEqual([(c["method"], c["path"]) for c in f.calls],
                         [("PUT", "/me/player/pause"), ("POST", "/me/player/next"),
                          ("POST", "/me/player/previous"), ("PUT", "/me/player/play")])
        self.assertFalse(run(spotify.control("shuffle"))["ok"])

    def test_play_with_nothing_active_goes_to_the_tv(self):
        self.sign_in()
        f = self.fake({("PUT", "/me/player/play"): [(404, {"error": {"message": "No active device"}}), (204, None)],
                       ("GET", "/me/player/devices"): [(200, {"devices": TestDevices.DEVICES})]})
        self.assertTrue(run(spotify.control("play"))["ok"])
        self.assertEqual(f.calls[-1]["query"], {"device_id": "t1"})

    def test_next_with_nothing_playing(self):
        self.sign_in()
        self.fake({("POST", "/me/player/next"): [(404, None)]})
        got = run(spotify.control("next"))
        self.assertFalse(got["ok"])
        self.assertIn("Nothing is playing", got["error"])


class TestRouteWiring(unittest.TestCase):
    """server.py, read as text (it needs aiohttp and the TV libraries to import): the sign-in routes sit
    outside /api/ (no pairing key), go through the loopback check, and come before the catch-all."""

    def test_wiring(self):
        with open(os.path.join(HERE, "..", "bridge", "server.py")) as fh:
            src = fh.read()
        app = src[src.index("def make_app():"):]
        for route in ('r.add_get("/spotify/login", h_spotify_login)', 'r.add_get("/spotify/callback", h_spotify_callback)',
                      'r.add_get("/api/spotify", h_spotify)', 'r.add_post("/api/spotify/play", h_spotify_play)',
                      'r.add_post("/api/spotify/control", h_spotify_control)'):
            self.assertIn(route, app)
            self.assertLess(app.index(route), app.index('r.add_get("/{path:.*}"'))
            self.assertLess(app.index(route), app.index('r.add_route("*", "/api/{tail:.*}"'))
        self.assertIn("spotify.start_login(request.remote)", src)
        self.assertIn("spotify.finish_login(request.remote, request.query)", src)


try:
    import aiohttp  # noqa: F401
    HAVE_AIOHTTP = True
except ImportError:
    HAVE_AIOHTTP = False


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp is not installed here (it is in the bridge's .venv)")
class TestRoutes(Base):
    """The routes in server.py: /spotify/login from the computer itself, refused from the network."""

    def _req(self, path, peer):
        from unittest.mock import MagicMock
        from aiohttp.test_utils import make_mocked_request
        transport = MagicMock()
        transport.get_extra_info.side_effect = lambda k, d=None: (peer, 50000) if k == "peername" else d
        return make_mocked_request("GET", path, transport=transport)

    def test_login_route(self):
        from aiohttp import web
        import server
        with self.assertRaises(web.HTTPFound) as e:
            run(server.h_spotify_login(self._req("/spotify/login", "127.0.0.1")))
        self.assertTrue(e.exception.location.startswith(spotify.AUTH_URL))
        resp = run(server.h_spotify_login(self._req("/spotify/login", "192.0.2.20")))
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
