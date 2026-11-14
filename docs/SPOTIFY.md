# Spotify on the TV home

With this switched on, the bridge can show what you played lately on Spotify and what is playing
now, and it can play a song on the TV: it opens Spotify on the TV by itself if it is not open yet,
then starts the song there (Spotify calls this Spotify Connect).

It needs:

- **Spotify Premium.** Spotify only lets Premium accounts start and control music from another app.
- **The bridge** (a computer at home that stays on). Every step below is done on that computer.
- **About 10 minutes, once.** You make your own small "app" on Spotify's developer site. It is free
  and it is only for you; nobody else sees it.

Nothing here asks for your Spotify password. You sign in on Spotify's own page, and the bridge only
keeps a key that Spotify gives it (in `bridge/data/spotify.json`, which stays on this computer and is
never uploaded anywhere).

## Step 1. Make your Spotify app

1. On the bridge computer, open <https://developer.spotify.com/dashboard> and log in with your
   **Premium** Spotify account. The first time, Spotify asks you to accept its developer terms.
2. Click **Create app**.
3. Fill it in:
   - **App name:** anything, for example `Couch TV`.
   - **App description:** anything, for example `My TV at home`.
   - **Redirect URIs:** type exactly this, then click **Add**:

     ```
     http://127.0.0.1:5400/spotify/callback
     ```

     It must be `127.0.0.1`, not `localhost` (Spotify no longer accepts `localhost`). If your
     bridge uses a port other than 5400, use that number instead.
   - Under **Which API/SDKs are you planning to use?**, tick **Web API**.
4. Tick the box to agree to Spotify's terms and click **Save**.

## Step 2. Copy the Client ID

On your new app's page, open **Settings**. Under **Basic Information** you see the **Client ID**, a
long mix of letters and numbers. Copy it. (You do **not** need the Client secret. Leave it alone.)

## Step 3. Give the Client ID to the bridge

Open **Terminal** on the bridge computer. Copy the whole block below, but first replace
`PASTE_CLIENT_ID_HERE` with the Client ID you copied (keep the quotes around it). Then paste it into
Terminal and press Return:

```sh
cd ~/couch-tv && .venv/bin/python - <<'EOF'
import json
path = "config.json"
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("spotify", {})["client_id"] = "PASTE_CLIENT_ID_HERE"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("Saved. Spotify is set up in config.json.")
EOF
```

If your couch-tv folder is somewhere other than your home folder, change `~/couch-tv` to where it is.

Now restart the bridge so it reads the new setting:

- If the bridge runs in a Terminal window that starts it again when it stops, run:

  ```sh
  pkill -f "bridge/server.py"
  ```

  It comes back by itself in about 5 seconds.
- If the bridge runs as a background service (README step 5), run:

  ```sh
  launchctl kickstart -k gui/$(id -u)/app.couchtv.bridge
  ```

## Step 4. Connect Spotify (once)

On the bridge computer itself (not your phone), open this address in a web browser:

```
http://127.0.0.1:5400/spotify/login
```

Spotify asks whether to allow your app to see what you play and control playback. Click **Agree**.
You land on a page that says **"Spotify is connected. You can close this tab."** That's it.

This page only opens on the bridge computer itself. From a phone or another computer it says no, on
purpose, so nobody else on the Wi-Fi can connect their Spotify to your TV.

If it says the link is old, just open the address again (each sign-in link works once, for 10
minutes).

## Step 5. Check it works

In Terminal on the bridge computer, run this. It reads your pairing key from `config.json` by
itself, so there is nothing to paste:

```sh
cd ~/couch-tv && KEY=$(.venv/bin/python -c 'import json; print(json.load(open("config.json")).get("bridge", {}).get("pairing_key", ""))') && curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:5400/api/spotify; echo
```

You should see `"connected": true` and a list of songs you played lately under `"recent"`. If you see
`"connected": false`, the `"reason"` next to it says what is missing, in plain words.

## Who can use it

Your Spotify app starts in what Spotify calls **Development Mode**. In that mode:

- The account that made the app must have **Premium**.
- **Up to 5 Spotify accounts** can use it, and each one has to be added by name first.
- The bridge is connected to **one** Spotify account at a time: the one that did Step 4.

If someone else at home wants their own account on it, add them: on your app's page open
**User Management**, type their name and the email address of their Spotify account, and click
**Add user**. They also need Premium. Then they do Step 4 on the bridge computer with their own
Spotify login (this replaces your sign-in on the bridge).

## If something does not work

- **"Spotify did not show up on the TV":** open Spotify on the TV once with the TV remote and check
  it is signed in to the **same** Spotify account as Step 4. Spotify only lists the TV while its app
  is open, so the bridge opens it for you, and then waits up to 20 seconds for it.
- **It plays on the wrong device:** add `"device_name"` with part of the TV's name as Spotify shows it
  (in the Spotify app on your phone, tap the speaker icon to see the names), for example
  `"spotify": {"client_id": "...", "device_name": "Samsung"}`, then restart the bridge.
- **"Spotify's sign-in ran out":** do Step 4 again.
- **"Premium is needed":** Spotify only allows starting and controlling music for Premium accounts.
- **To switch it off:** remove the `"spotify"` part from `config.json`, delete
  `bridge/data/spotify.json`, and restart the bridge. You can also delete the app on
  <https://developer.spotify.com/dashboard>, and remove its access under your Spotify account's
  **Manage apps** page.

## For developers

The bridge side is `bridge/spotify.py` (setup notes in its first lines) and the routes in
`bridge/server.py`: `GET /api/spotify`, `POST /api/spotify/play`, `POST /api/spotify/control`
(see [bridge/API.md](../bridge/API.md)), and the sign-in pages `GET /spotify/login` and
`GET /spotify/callback`. Sign-in is Spotify's Authorization Code flow with PKCE, so there is no client
secret anywhere. Opening Spotify on the TV uses the TV's own app launch (on a Samsung, Tizen id
`3201606009684` from `bridge/apps.py`).

Spotify endpoints used, all still open to Development Mode apps after Spotify's February 2026
changes: `/api/token`, `GET /me/player/recently-played`, `GET /me/player`, `GET /me/player/devices`,
`PUT /me/player/play`, `PUT /me/player/pause`, `POST /me/player/next`, `POST /me/player/previous`,
and `PUT /me/player` (moving playback to the TV). If Spotify ever answers one of the reads with 403,
404 or 410, the card shows less instead of breaking, and the bridge log says so once.
