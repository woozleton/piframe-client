# PiFrame Client

`piframe_client.py` is the client that connects to the PiFrame Manager server over WebSocket and renders all media through a single Chromium kiosk session running under `cage`.

This client now uses one browser-based renderer for:

- single videos
- video playlists
- image slideshows
- mixed image/video playlists
- idle fallback media

## Files

- `piframe_client.py` - WebSocket client + browser orchestrator + audio companion sidecar
- `browser_renderer_template.py` - Chromium kiosk HTML/JS template
- `update.sh` - in-place self-updater (see Self-update below)
- `.gitattributes` - pins shell + Python files to LF endings
- `requirements.txt`
- `scripts/bootstrap_pi.sh`
- `idle.jpg`, `idle.html` - idle fallback (HTML preferred when present)
- `/etc/systemd/system/piframe-client.service` (installed by bootstrap)

## How It Works

At startup, the client:

1. connects to the server over WebSocket
2. starts a single Chromium kiosk session under `cage`
3. writes a local HTML renderer to `/tmp/piframe_browser.html`
4. writes browser state to `/tmp/piframe_browser_state.json`
5. updates that state whenever the server sends a playback command
6. binds a tiny loopback HTTP server on `127.0.0.1:18888` (configurable via
   `PIFRAME_BROWSER_EVENT_PORT`) that the kiosk JS POSTs to whenever it
   advances a slide or toggles pause - lets the manager UI reflect the
   actual on-screen state instead of guessing from playback timestamps

The browser polls the state file and renders media fullscreen on the
attached display. State changes (pause / resume / slide rotation) wake
the status loop immediately so the manager sees them within ~one network
round-trip instead of the periodic 2-second backstop.

## Supported Commands

The client currently handles these server-side commands:

- `play`
- `video_playlist`
- `audio_playlist` (direct audio + companion mode via `is_companion: true`)
- `slideshow`
- `pause`
- `next`
- `previous`
- `stop` (also accepts `is_companion: true` to halt only the companion)
- `volume`
- `update_self` (see Self-update below)

Single-video note:

- the server may normalize a bare `play` request into a one-item `video_playlist`
- the client detects that one-item video playlist case and renders it locally as a true single video

Audio companion note:

- when `audio_playlist` arrives with `is_companion: true`, the client routes the audio to a sidecar `mpv` process instead of replacing the visual content
- the visual `<video>` is muted (when `mute_visual: true`) so the companion is the only audio source the operator hears
- see [Audio](#audio) for the OS-level audio mixer requirement

## Display Features

Current browser renderer features include:

- 270-degree rotation for portrait-mounted displays
- crossfade-style transitions using double-buffered stages
- mixed-media playlist support
- hidden cursor via compositor-level pointer parking with `wlrctl`
- idle fallback image when nothing is playing
- top-of-screen rotated status banner for runtime issues
- bottom OSD for pause / volume / mute state

## Media Guidance

For this Raspberry Pi 5 browser renderer, the practical house format is:

- `1080p`
- `H.264`
- moderate bitrate
- audio supported through the Pi's active HDMI ALSA output

What we observed in testing:

- `1080p` playback is solid
- Chromium-based mixed-media rendering is efficient enough for production use
- `4K` video is not a good fit for this Pi in the Chromium kiosk path and can saturate CPU

If you need to keep 4K masters in the library, the recommended approach is to generate playback-optimized derivatives for the Pi clients.

## Service

Installed service file:

- [piframe-client.service](/etc/systemd/system/piframe-client.service)

Current service configuration:

```ini
[Unit]
Description=PiFrame Client
After=network-online.target mnt-nas.mount
Wants=network-online.target mnt-nas.mount

[Service]
User=woozleton
WorkingDirectory=/home/woozleton/piframe_client
ExecStart=/home/woozleton/piframe_client/api-env/bin/python /home/woozleton/piframe_client/piframe_client.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PIFRAME_SERVER=ws://...
Environment=PIFRAME_NAS_ROOT=/mnt/nas

[Install]
WantedBy=multi-user.target
```

The `Wants=` (vs `Requires=`) on `network-online.target` and the NAS
mount unit lets the service come up at boot even when those have a
slow / degraded start. The client itself reconnects on its own retry
loop and tolerates a briefly-unavailable NAS, so soft dependencies
are the right shape.

User lingering must be enabled for the service user so
`/run/user/<uid>` exists at boot for cage / Chromium's Wayland
socket. `bootstrap_pi.sh` runs `loginctl enable-linger <user>`
automatically. On a Pi bootstrapped before that change was added,
run it manually once:

```bash
sudo loginctl enable-linger $USER
```

Useful commands:

```bash
sudo systemctl restart piframe-client
sudo systemctl status piframe-client --no-pager
journalctl -u piframe-client -f
```

## Self-update

The server's System tab has a "Client updates" card that pushes
`git pull` + service restart to any connected Pi over the existing
WebSocket. There is no SSH or per-Pi credential involved - the Pi
authenticates to GitHub directly to fetch from `origin/main`.

Flow:

1. operator clicks Update in the manager UI
2. server sends an `update_self` WebSocket command
3. client spawns `update.sh` detached and continues running until
   systemd restarts it
4. `update.sh` runs `git fetch origin main && git reset --hard
   origin/main`, writes a marker file describing what changed, then
   `exec sudo systemctl restart piframe-client`
5. systemd respawns the client; on boot it reads the marker file,
   ships it home in the next status heartbeat, and deletes it

The marker carries:

- from/to short SHAs
- list of changed files
- one-line subjects for each new commit
- `noop=true` flag if before==after (skips the restart entirely)

Marker location:

- `/tmp/piframe_last_update.json` (consumed by client on first read)

Update script log (every run, even silent failures):

- `/tmp/piframe_update.log`

Heartbeat fields the server cares about:

- `client_version` - short SHA, computed once at boot
- `last_update` - whatever the marker contained, echoed every tick
  until the server's TTL clears the result UI-side

### Sudoers prerequisite

`update.sh` ends with `exec sudo /bin/systemctl restart
piframe-client`, so the service user needs passwordless sudo for that
command. The simplest setup (already in place on existing devices)
is `/etc/sudoers.d/010_pi-nopasswd`:

```
woozleton ALL=(ALL) NOPASSWD: ALL
```

A tighter alternative scoped just to the restart:

```
woozleton ALL=NOPASSWD: /bin/systemctl restart piframe-client
```

### Line-ending guard

`.gitattributes` pins `*.sh` and `*.py` to LF. Without it, a commit
from a Windows checkout (where `core.autocrlf=true` is the default)
rewrites the script with CRLF on commit; the Pi checks it back out
with LF and `git status --porcelain` then reports `update.sh` as
permanently "modified," tripping `update.sh`'s own dirty-tree guard
on every subsequent self-update. The script also runs `git config
--local core.autocrlf input` defensively on each invocation.

## Replicating To Another Pi

```bash
sudo apt-get update
sudo apt-get install -y gh git
```

Then clone and bootstrap:

```bash
git clone https://github.com/woozleton/piframe-client.git /home/<user>/piframe_client
cd /home/<user>/piframe_client
sudo ./scripts/bootstrap_pi.sh --user <user> --server ws://<manager-ip>:8080/ws
```

What it does:

- installs `gh` and `git`
- installs required apt packages (chromium, cage, seatd, wlrctl,
  alsa-utils, mpv for the audio companion)
- installs and enables `seatd`
- installs `wlrctl` for compositor-level cursor parking
- creates `/home/<user>/piframe_client/api-env`
- installs Python requirements from `requirements.txt`
- writes `/etc/systemd/system/piframe-client.service`
- enables user lingering via `loginctl enable-linger` so
  `/run/user/<uid>` exists at boot for cage / Chromium's Wayland
  socket
- enables and restarts the service

Audio mixer (one-time, recommended):

```bash
sudo apt install pipewire pipewire-pulse wireplumber
systemctl --user --now enable pipewire pipewire-pulse wireplumber
```

Without a userspace mixer, the audio companion stays silent while
the surface is showing video. See [Audio](#audio).

Useful flags:

- `--nas-root /mnt/nas`
- `--mount-unit mnt-nas.mount`
- `--skip-apt`

## Logging

The client writes structured operational logs to `journalctl`.

Typical events include:

- `client_starting` (carries `audio_server=` so you can see the
  detected mixer at a glance)
- `audio_mixer_warning` (only when no mixer detected)
- `registered`
- `play_command`
- `video_playlist_command`
- `audio_playlist_command` / `audio_companion_command`
- `slideshow_command`
- `companion_mpv_started` / `companion_mpv_loaded` /
  `companion_mpv_stopped`
- `browser_state_updated`
- `renderer_transition`
- `websocket_closed`

Chromium and `cage` output is redirected away from `journalctl` into:

- `/tmp/piframe_browser.log`

That browser log rotates by size:

- active log: `/tmp/piframe_browser.log`
- backups: `/tmp/piframe_browser.log.1`, `.2`, `.3`

Default rotation settings:

- max size: `5 MB`
- backups kept: `3`

Self-update output (every run, append-only):

- `/tmp/piframe_update.log`

Useful when an update appears to silently fail - the client spawns
`update.sh` with stdout/stderr piped to `/dev/null`, so this is the
only place to see what the script actually did.

## Audio

The client uses two audio paths depending on the dispatch:

- **Visual content audio** (video, direct audio playback) plays
  through Chromium's `<video>` element straight to the OS audio
  device.
- **Audio companion** plays through a sidecar `mpv` process the
  client spawns on demand. Companion items run in parallel with
  whatever visual content is on screen so the operator hears music
  over images / silent videos / muted videos.

### Why a separate mpv process

Chromium on Pi keeps the OS audio device locked while a `<video>`
element is playing - even when muted. An in-page `<audio>` element
can't claim the device in that state, which made the original
companion implementation silent in mid-stream toggles. mpv opens
its own audio stream which the OS audio server mixes with
Chromium's at the kernel level.

### OS-level audio mixer requirement

**Bare ALSA is single-stream**: whichever process opens the audio
device first holds it exclusive, the second gets silence. For
companion playback to work alongside a Chromium `<video>`, the Pi
needs a userspace mixer:

- **PipeWire** (recommended, modern default on Raspberry Pi OS):

```bash
sudo apt install pipewire pipewire-pulse wireplumber
systemctl --user --now enable pipewire pipewire-pulse wireplumber
```

- **PulseAudio** also works.

- **ALSA dmix** as a last resort - configure `~/.asoundrc` with a
  dmix plugin pointing at the HDMI device.

The client logs the detected mixer on startup under `audio_server=`.
If it logs `audio_server=alsa-only`, an `audio_mixer_warning` event
follows explaining what to install.

### Companion mpv

- binary: `mpv` (path overridable via `PIFRAME_COMPANION_MPV_BIN`)
- IPC socket: `/tmp/piframe_companion_mpv.sock` (overridable via
  `PIFRAME_COMPANION_MPV_SOCKET`)
- log: `/tmp/piframe_companion_mpv.log`
- volume: tracks the surface volume the operator sets via the
  manager UI (forwarded on every `volume` command)

### Per-Pi setup

- ALSA default output must point at the active HDMI device when
  using bare ALSA (not needed with PipeWire/PA, which auto-route)
- on this Pi the historical setup uses `/home/woozleton/.asoundrc`
  with `plughw:0,0`

If video is playing but audio is missing, verify the audio mixer
state in this order:

```bash
systemctl --user status pipewire pipewire-pulse 2>&1 | head
pactl info 2>&1 | head
aplay -l
```

## Persisted Client Settings

The client remembers the most recent local volume/mute state across service restarts and reboots.

Persisted settings file:

- `/home/woozleton/piframe_client/client_settings.json`

Currently persisted:

- `volume`
- `muted`

Explicit manager volume commands still override the saved value.

## Git / GitHub

This project folder is intended to be self-contained for source control:

- source lives in `/home/woozleton/piframe_client`
- the local Python runtime also lives in this folder as `api-env/`
- `api-env/` should not be committed

Tracked files:

- `piframe_client.py`
- `browser_renderer_template.py`
- `update.sh`
- `.gitattributes`
- `requirements.txt`, `requirements.md`
- `scripts/bootstrap_pi.sh`
- `idle.jpg`, `idle.html`
- `README.md`

Ignored:

- `api-env/` (local Python venv)
- `client_settings.json` (per-device runtime state)
- `__pycache__/` and Python bytecode
- local `*.log` files

## On-Screen Status Banner

The browser renderer can show a rotated top-of-screen banner for important runtime issues without taking over the entire display.

Current banner cases include:

- `NAS unavailable`
- `Media file missing`
- `Website unavailable`
- `Server disconnected`

Behavior:

- the banner overlays current content
- it clears automatically when valid content resumes
- it is sized and rotated for the portrait-mounted display

## On-Screen Display

The browser renderer also shows a transient bottom OSD for playback controls.

Current OSD cases include:

- pause
- volume changes
- mute

Behavior:

- uses inline SVG icons
- animates in/out
- auto-hides after about 1 second

## Important Environment Variables

These can be set in the service file or shell environment.

### Core

- `PIFRAME_SERVER`
- `PIFRAME_NAS_ROOT`
- `PIFRAME_IDLE_MEDIA`
- `PIFRAME_CHROMIUM_BIN`
- `PIFRAME_CAGE_BIN`
- `PIFRAME_COMPANION_MPV_BIN` (default `mpv`) - sidecar binary for the audio companion
- `PIFRAME_COMPANION_MPV_SOCKET` (default `/tmp/piframe_companion_mpv.sock`)

### Self-update

- `PIFRAME_SERVICE_NAME` (default `piframe-client`) - the systemd
  unit `update.sh` restarts after a successful pull
- `PIFRAME_LAST_UPDATE_FILE` (default `/tmp/piframe_last_update.json`)
- `PIFRAME_UPDATE_LOG` (default `/tmp/piframe_update.log`)

### Browser Renderer

- `PIFRAME_BROWSER_SHOW_HUD`
- `PIFRAME_BROWSER_TRANSITION`
- `PIFRAME_BROWSER_TRANSITION_DURATION_MS`
- `PIFRAME_BROWSER_VIDEO_FILL_MODE`
- `PIFRAME_BROWSER_LOG_MAX_BYTES`
- `PIFRAME_BROWSER_LOG_BACKUPS`
- `PIFRAME_BROWSER_EVENT_PORT` (default `18888`) - loopback port the
  kiosk JS POSTs slide-change and pause events to so they reach the
  manager in real time

### Current Useful Values

`PIFRAME_BROWSER_VIDEO_FILL_MODE`:

- `contain`
- `cover`

`PIFRAME_BROWSER_TRANSITION`:

- currently implemented and used as `fade`

## Notes

- This client expects the NAS to be mounted before the service starts.
- The service currently depends on `mnt-nas.mount`.
- Browser state and runtime files live in `/tmp`.
- The renderer is intentionally lightweight and avoids desktop-session dependencies beyond what `cage` and Chromium need.
- The Python runtime is kept inside the project folder at `/home/woozleton/piframe_client/api-env`.
- The stable video fill mode is `contain`.

## Troubleshooting

If nothing appears on screen:

1. check service logs:

```bash
journalctl -u piframe-client -f
```

2. check browser-side logs:

```bash
tail -f /tmp/piframe_browser.log
```

3. verify the service environment:

- `XDG_RUNTIME_DIR=/run/user/1000`
- NAS mount is available
- Chromium and `cage` are installed

4. verify user lingering is enabled (cage needs `/run/user/<uid>`
   at boot for the Wayland socket):

```bash
loginctl show-user $USER --property=Linger
# expected: Linger=yes
sudo loginctl enable-linger $USER  # if not yes
```

If you see `cage[<defunct>]` in `ps` and `Unable to open Wayland
socket: Invalid argument` in `/tmp/piframe_browser.log`, lingering
is missing.

5. restart the service:

```bash
sudo systemctl restart piframe-client
```

If audio companion is silent while video plays:

1. check the detected mixer in the boot log:

```bash
journalctl -u piframe-client -b | grep audio_server
```

2. if it reads `audio_server=alsa-only`, install PipeWire (see
   [Audio](#audio)).

3. confirm mpv companion is being spawned:

```bash
journalctl -u piframe-client -b | grep companion_mpv
# expect: companion_mpv_started, companion_mpv_loaded events
tail -50 /tmp/piframe_companion_mpv.log
```
