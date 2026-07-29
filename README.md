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
- `vendor/butterchurn/` - Butterchurn (MilkDrop port) + preset bundle for the audio visualizer overlay
- `vendor/h264ify/` - bundled Chromium extension that forces video sites
  off AV1 in webview mode (see [Remote Control](#remote-control-vnc))
- `update.sh` - in-place self-updater (see Self-update below)
- `.gitattributes` - pins shell + Python files to LF endings
- `requirements.txt`
- `scripts/bootstrap_pi.sh`
- `scripts/test_webview.sh` - manual webview-mode test driver (see [Remote Control](#remote-control-vnc))
- `idle.jpg`, `idle.html` - idle fallback (HTML preferred when present)
- `/etc/systemd/system/piframe-client.service` (installed by bootstrap)
- `/etc/systemd/system/piframe-vnc.service` (installed by bootstrap; runs `wayvnc` against the cage Wayland session)

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
   actual on-screen state instead of guessing from playback timestamps.
   The same endpoint also accepts manager-shaped commands at
   `POST /control` for local automation (see
   [Remote Control](#remote-control-vnc))
7. starts a sibling `wayvnc` instance (separate systemd unit) that
   attaches to the same cage Wayland session, so an operator can
   drive the screen with mouse/keyboard from any VNC viewer on the
   LAN (see [Remote Control](#remote-control-vnc))

The browser polls the state file and renders media fullscreen on the
attached display. State changes (pause / resume / slide rotation) wake
the status loop immediately so the manager sees them within ~one network
round-trip instead of the periodic 2-second backstop.

## Supported Commands

The client currently handles these server-side commands:

- `play`
- `sync_video` (mural / clock-synced video; see [below](#mural--clock-synced-video))
- `video_playlist`
- `audio_playlist` (direct audio + companion mode via `is_companion: true`)
- `slideshow`
- `pause`
- `next`
- `previous`
- `stop` (also accepts `is_companion: true` to halt only the companion)
- `volume`
- `update_self` (see Self-update below)
- `webview_open` (optional `url` field; see [Remote Control](#remote-control-vnc))
- `webview_close`

Single-video note:

- the server may normalize a bare `play` request into a one-item `video_playlist`
- the client detects that one-item video playlist case and renders it locally as a true single video

Audio companion note:

- when `audio_playlist` arrives with `is_companion: true`, the client routes the audio to a sidecar `mpv` process instead of replacing the visual content
- the visual `<video>` is muted (when `mute_visual: true`) so the companion is the only audio source the operator hears
- see [Audio](#audio) for the OS-level audio mixer requirement

## Clock-Synced Slideshows

The `slideshow` command may carry an additive `sync` object:

```json
{ "images": ["... full playlist, server-authored order ..."],
  "interval": 8.0,
  "shuffle": false,
  "sync": { "mode": "clock", "length": 45 } }
```

When present and valid (`mode == "clock"`, `length` a positive integer equal
to the row length, valid interval), the kiosk runs the slideshow in
wall-clock lockstep instead of free-running:

- the displayed index is ALWAYS `floor(Date.now() / 1000 / interval) % length`,
  recomputed at every flip (never incremented), so a pause, hang, reboot,
  or missed server push self-heals at the next boundary
- flips happen exactly at wall-clock multiples of `interval` (the fleet's
  screens advance together; each screen holds a different server-authored
  permutation of the same playlist, so no two screens ever show the same
  image at the same moment)
- the server re-sends a freshly shuffled row each lap
  (`length * interval` seconds); the client swaps the row in place with a
  normal cross-fade - no restart at index 0. If the push never arrives
  (server down), the client keeps indexing its old row by the clock:
  still collision-free, just repeating the same lap order
- `shuffle` is always `false` in sync mode - the row order is
  authoritative and is never reordered client-side
- `next`/`previous` show the adjacent item as a momentary "peek"; the next
  wall-clock boundary snaps back to the clock slot. `pause` freezes the
  current image; resume snaps to the clock
- a malformed `sync` object is dropped (logged as `slideshow_sync_ignored`)
  and the slideshow free-runs exactly as it does for servers that never
  send the key

Requires NTP-synced clocks (standard Raspberry Pi OS setup). The
server-side model (timetable generation, dedup registry, degradation
ladder) is documented in the manager repo at
`docs/subsystems/display-sync.md`.

## Mural / Clock-Synced Video

The `sync_video` command plays one looping video slaved to the same NTP
wall clock the slideshow timetable uses, so several screens can each show
a pre-cropped view of a single master video in lockstep (a creature
crosses seamlessly from one screen to the next). It is a sibling of
`play`, not a slideshow mode - the two clock paths are deliberately kept
separate (`sync.mode == "clock_video"` here vs `"clock"` for slideshows).

```json
{ "cmd": "sync_video",
  "params": { "url": "/mnt/nas/_mural/test/frame-livingroom-left.mp4",
              "duration": 60.0, "epoch": 1785300000.0,
              "latency_ms": 0, "show": "test" } }
```

- `url` - the per-surface video (normalized like `play`); always looped
- `duration` - master video length in seconds (must equal the file's)
- `epoch` - wall-clock UNIX epoch (float) the show "started"; may be in
  the past and is never waited on
- `latency_ms` - per-screen display-lag compensation (either sign,
  default `0`)
- `show` - display name; labels the surface as `Mural: <show>`

Every 500ms the kiosk disciplines the active `<video>` toward
`target = ((now - epoch + latency_ms / 1000) mod duration)`:

- `|err| <= 0.04s`: playback rate `1.0` (deadband)
- `0.04 < |err| <= 0.5s`: rate nudged within +-3% (invisible)
- `|err| > 0.5s`: hard seek to `target` (rare - only at join or after a
  gross stall)

`err` is measured loop-seam-safe, so the wrap at the loop point never
triggers a spurious seek. There is no start coordination: a screen that
joins late or stalls converges onto the shared clock within ~1-2s, and a
dropped frame never accumulates error. A re-dispatch with a new `epoch`
converges without re-loading the video. An operator `pause` freezes the
video and the discipline no-ops; resume converges for free. A malformed
command (bad `duration`/`epoch`/`url`) is dropped (logged as
`sync_video_invalid`) and playback is left untouched. Any other playback
command (`play`, `slideshow`, `stop`, `webview_open`, ...) exits mural
mode.

While mural is active the client reports `playback_state: "mural"` in its
heartbeat (the manager's schedule / companion guards key on it). Requires
NTP-synced clocks, exactly like the clock-synced slideshow above. The
server-side model is documented in the manager repo at
`docs/plans/mural.md`.

## Display Features

Current browser renderer features include:

- compositor-level rotation for portrait-mounted displays (`wlr-randr`
  applied to cage at startup, see [Remote Control](#remote-control-vnc))
- crossfade-style transitions using double-buffered stages
- mixed-media playlist support
- hidden cursor via compositor-level pointer parking with `wlrctl`
- idle fallback image when nothing is playing
- top-of-screen status banner for runtime issues
- bottom OSD for pause / volume / mute state
- audio visualizer overlay (Butterchurn / MilkDrop) during audio
  playback, with per-playlist preset selection and a track-name
  OSD pulse on each new song

## Remote Control (VNC)

Each Pi runs a `wayvnc` instance attached to the cage Wayland session
so an operator can drive the kiosk screen with mouse and keyboard
from any VNC viewer on the LAN. This is useful for:

- interacting with arbitrary websites loaded into the kiosk
- nudging Chromium when a page needs a click to recover
- debugging what the kiosk is actually rendering, in real time

### Architecture

- `wayvnc` runs under the same user as `cage` (e.g. `woozleton`) so
  it captures the active kiosk output instead of starting a headless
  session of its own
- managed by a dedicated systemd unit (`piframe-vnc.service`) so it
  starts at boot alongside `piframe-client.service`
- listens on `0.0.0.0:5900` by default
- the Raspberry Pi OS package ships its own `wayvnc.service` running
  as user `vnc` against a private runtime dir; bootstrap masks that
  unit because it would not capture the kiosk

### Why compositor-side rotation

The kiosk renderer used to apply a 270-degree CSS rotation so a
portrait-mounted display read upright while cage produced a landscape
framebuffer. That worked for the Pi's HDMI output, but VNC clients
mirror what cage actually produces - so a remote viewer would see a
landscape framebuffer with sideways content, and most VNC clients
(RealVNC Viewer, TigerVNC, macOS Screen Sharing, Remmina) do not
expose a client-side rotation toggle.

Rotating at the compositor instead (`wlr-randr --transform 90` on
the cage output) makes the framebuffer itself portrait, so:

- the physical TV reads upright
- VNC viewers see the screen upright with no client-side rotation
- the renderer no longer applies a CSS rotation
  (`BROWSER_ROTATION_DEGREES = 0`)

The transform direction (90, 180, 270) is configurable via the
`PIFRAME_OUTPUT_TRANSFORM` environment variable. Default is `90`.

### Connecting

Any VNC viewer that speaks RFB will work. Tested setups:

- iPhone: RealVNC Viewer (App Store) - point it at
  `<pi-ip>:5900`, no password by default
- macOS: Finder -> `Cmd+K` -> `vnc://<pi-ip>:5900`
- Linux desktop: TigerVNC, Remmina
- Windows: TightVNC, RealVNC

### Auth and trust posture

V1 ships with `enable_auth=false` to match the existing LAN-trust
posture: the manager -> client WebSocket has no per-device tokens
either. Do not expose `5900/tcp` outside the LAN.

To turn on authentication later, edit
`/home/<user>/.config/wayvnc/config` and follow `man wayvnc` -
`enable_auth=true` requires TLS keys + a username/password (the
packaged `wayvnc-generate-keys.sh` covers the keys).

### Useful checks

```bash
systemctl status piframe-vnc --no-pager
journalctl -u piframe-vnc -f
ss -ltnp | grep 5900
```

### Compositor compatibility

Cage exposes the wlroots protocols `wayvnc` requires
(`wlr-screencopy-unstable-v1`, `wlr-virtual-pointer-unstable-v1`,
`virtual-keyboard-unstable-v1`). Verified on cage 0.x running on
Raspberry Pi OS Trixie with the Pi 5's V3D path.

### Webview mode (operator-driven web browsing)

VNC alone only lets you click on whatever the kiosk happens to be
showing - and the kiosk normally renders its own self-generated
HTML, which has nothing meaningful to click. Webview mode swaps the
kiosk Chromium for a windowed Chromium with chrome (address bar +
tabs) so an operator on VNC can browse arbitrary websites.

Switching modes restarts Chromium under cage with a different arg
set; `--kiosk` is mutually exclusive with showing chrome, so a
process restart is the only way to toggle. The TV shows ~2-3s blank
during each transition.

Commands:

- `webview_open` (optional `url`) - tear down the kiosk Chromium,
  start a windowed Chromium pointed at the URL (or `about:blank` so
  the operator types it via VNC). Stops the audio companion as a
  side effect since the operator's intent is a clean web session.
- `webview_close` - tear down the windowed Chromium, restart the
  kiosk renderer, return to idle. Playback does not auto-resume -
  the operator picks the next item from the manager.

Heartbeat fields the manager reads to reflect mode:

- `browser_mode` - `"kiosk"` or `"webview"`
- `webview_url` - the URL the windowed Chromium loaded (null in
  kiosk mode)
- `playback_state` - reads `"webview"` while in webview mode

Notes:

- The Chromium user data dir is shared across modes, so cookies,
  history, bookmarks, and saved passwords persist when toggling
- The compositor-side rotation stays applied in webview mode, so
  websites render in the same portrait orientation as the kiosk
  (Chromium's chrome adapts; most modern sites adapt; a few will
  look awkward in portrait)
- The VNC viewer disconnects briefly during the mode swap and most
  clients auto-reconnect. Connecting only after the mode change
  avoids the disconnect entirely.
- A bundled Chromium extension at `vendor/h264ify/` is auto-loaded
  in webview mode (only). It overrides
  `MediaSource.isTypeSupported` to return false for AV1, so video
  sites (YouTube, Vimeo, Twitch, etc.) fall back to VP9 / H.264.
  Measured impact on a Pi 5 playing YouTube 1080p60: CPU went
  from ~80% (AV1 / libdav1d) to ~60% (VP9), and the visual
  experience is meaningfully smoother. The Pi 5 has no AV1 / VP9
  / H.264 hardware decode block at all (only HEVC), so all video
  decode is on the CPU; AV1 is just the most expensive of the
  three. 1080p60 is still software-bound after the swap and
  YouTube's "dropped frames" counter remains high - if smoothness
  matters more than 60fps, prefer 1080p30 sources. Kiosk mode
  does not load the extension because NAS-sourced playlist
  content is never AV1.

### Loopback control endpoint

The browser-event server on `127.0.0.1:18888` also accepts
manager-shaped commands at `POST /control`. Same in-process
dispatcher the manager WebSocket uses, so it's the fast path
(~700ms total mode swap on a Pi 5, measured) - no service
restart, no cage cold start.

Example:

```bash
curl -H 'Content-Type: application/json' \
  -d '{"cmd":"webview_open","params":{"url":"https://example.com"}}' \
  http://127.0.0.1:18888/control
curl -H 'Content-Type: application/json' \
  -d '{"cmd":"webview_close"}' \
  http://127.0.0.1:18888/control
```

Loopback-only (`127.0.0.1`), no auth - same security posture as
the existing browser-event endpoint. Useful for local automation
and for the test script below.

### Testing webview mode without the manager

`scripts/test_webview.sh` posts `webview_open` to the loopback
control endpoint and on Ctrl-C posts `webview_close`. Lets you
exercise the same in-process fast path the manager would use,
without needing the manager wired up.

```bash
sudo ./scripts/test_webview.sh                       # about:blank
sudo ./scripts/test_webview.sh https://youtube.com   # specific URL
sudo ./scripts/test_webview.sh --close               # close any active webview and exit
```

Requires `piframe-client.service` to be running. Mode swap takes
~700ms (same as the manager-driven path).

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

Two units are installed by `bootstrap_pi.sh`:

- [piframe-client.service](/etc/systemd/system/piframe-client.service) - the main client
- [piframe-vnc.service](/etc/systemd/system/piframe-vnc.service) - `wayvnc` attached to the cage session (see [Remote Control](#remote-control-vnc))

Current `piframe-client.service` shape:

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
Environment=PIFRAME_OUTPUT_TRANSFORM=90

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
4. `update.sh` archives any local drift into its logfile and clobbers
   it (`git clean -fd`, sparing ignored state like `api-env/` and
   `client_settings.json` - this checkout is a deployment artifact,
   not a workspace), runs `git fetch origin main` (bounded by
   `timeout 45` so a dead network path fails loudly instead of
   hanging) `&& git reset --hard origin/main`, writes a marker file
   describing what changed, then
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

### What self-update does NOT do

Self-update is `git pull` + service restart. It does not re-run
`scripts/bootstrap_pi.sh`, so changes that live outside the repo
checkout don't roll out via this path:

- new apt packages (e.g. `wayvnc`, `wlr-randr` introduced in the
  remote-control work)
- new systemd unit files (e.g. `piframe-vnc.service`)
- changes to the `piframe-client.service` unit itself (e.g. new
  `Environment=` lines like `PIFRAME_OUTPUT_TRANSFORM`)
- masking or unmasking system services (e.g. the packaged
  `wayvnc.service` we mask in favor of our own)
- generated config files like `~/.config/wayvnc/config`

When a release adds infrastructure of that shape, every existing
Pi needs `bootstrap_pi.sh` re-run once via SSH to pick it up.
After that, future code-only releases can be self-update only.

The bootstrap is idempotent: re-running on a configured Pi
overwrites the unit files in place, preserves
`PIFRAME_OUTPUT_TRANSFORM` from the existing service file (so the
operator's orientation choice survives), and skips the wayvnc
config write if one already exists. It does restart both
`piframe-client` and `piframe-vnc` at the end so there's a brief
blank screen.

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
permanently "modified" (historically that aborted every self-update;
today it just triggers the archive-and-clobber path, but the pin
keeps the noise out of the update log). The script also runs
`git config --local core.autocrlf input` defensively on each
invocation.

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
  wlr-randr, wayvnc, alsa-utils, mpv for the audio companion)
- installs and enables `seatd`
- installs `wlrctl` for compositor-level cursor parking
- installs `wlr-randr` so the client can rotate the cage output to
  match the physical mount (see [Remote Control](#remote-control-vnc))
- installs `wayvnc` and writes the `piframe-vnc.service` unit so the
  kiosk display is reachable from a VNC viewer on the LAN
- masks the packaged `wayvnc.service` (it runs as a separate `vnc`
  user against a private headless session, which would not capture
  the kiosk)
- creates `/home/<user>/piframe_client/api-env`
- installs Python requirements from `requirements.txt`
- writes `/etc/systemd/system/piframe-client.service`
- writes `/etc/systemd/system/piframe-vnc.service`
- writes `/home/<user>/.config/wayvnc/config` (only if absent, so
  later edits survive re-runs)
- enables user lingering via `loginctl enable-linger` so
  `/run/user/<uid>` exists at boot for cage / Chromium's Wayland
  socket
- enables and restarts both services

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
- `--orientation <name>` - one of `landscape`, `portrait` (default
  on first run), `portrait-ccw`, `upside-down`. If omitted the
  bootstrap prompts on first run and reuses the existing setting
  from the installed service file on re-runs.
- `--output-mode <mode>` - force the cage framebuffer mode via
  `wlr-randr --mode`. Empty / omitted = honor the TV's EDID-native
  mode. Useful on 4K TVs to drop output to 1080p so the visualizer
  + Chromium composite at 1/4 the pixels and the TV's built-in
  scaler upsamples to native. Example: `--output-mode 1920x1080@60`.
  Survives reboots + `update_self`; re-runs of bootstrap without
  the flag inherit the existing value from the service file.
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

### Required: PipeWire + pipewire-pulse

The companion path requires three pieces that work together:

```bash
sudo apt install pipewire pipewire-pulse wireplumber pulseaudio-utils
systemctl --user --now enable pipewire pipewire-pulse wireplumber
```

What each piece does:

- `pipewire` + `wireplumber` - the actual audio server that mixes
  multiple streams into one HDMI output. Without this, bare ALSA
  is single-stream and the second process opening the device gets
  silence.
- `pipewire-pulse` - the PulseAudio compatibility layer. mpv is
  configured to use `--ao=pulse` so it goes through this layer
  (which auto-routes the stream to the default sink). Without it
  mpv connects to PipeWire as a "raw" producer that isn't routed
  to any sink and the companion stays inaudible.
- `pulseaudio-utils` - provides `pactl`, used by the client to
  mute Chromium's sink-input when the override-embedded-audio
  toggle is on. Without `pactl` the helper bails silently and
  Chromium's video audio drowns out the companion.

PulseAudio classic also works as a substitute for `pipewire +
pipewire-pulse + wireplumber`, but PipeWire is the modern default
on Raspberry Pi OS.

The client logs the detected mixer on startup under `audio_server=`.
If it logs `audio_server=alsa-only`, an `audio_mixer_warning`
follows explaining what to install. `pactl` availability is
checked at mute time; missing-pactl shows up as
`companion_chromium_mute_failed` events.

### Companion mpv

- binary: `mpv` (path overridable via `PIFRAME_COMPANION_MPV_BIN`)
- IPC socket: `/tmp/piframe_companion_mpv.sock` (overridable via
  `PIFRAME_COMPANION_MPV_SOCKET`)
- log: `/tmp/piframe_companion_mpv.log`
- volume: tracks the surface volume the operator sets via the
  manager UI (forwarded on every `volume` command)
- audio output: `--ao=pulse` so PipeWire's auto-router connects
  it to the default HDMI sink. Without this flag mpv decodes
  audio but it's not wired to any sink (visible in `pw-link -l`
  - mpv's output ports show no `|->` connection)

### Override-embedded-audio path

When the operator toggles "Companion replaces video sound":

1. server marks the surface with `override_embedded_audio: true`,
   re-evaluates the routing, sends `audio_playlist` with
   `is_companion: true, mute_visual: true`
2. Pi-side `_set_companion_state` writes `video_mute_override`
   to the renderer state (so `<video>.muted` becomes true) AND
   calls `_set_chromium_sink_input_mute(true)` which runs
   `pactl set-sink-input-mute` against Chromium's PipeWire stream
3. companion mpv plays into HDMI via the same mixer

Both mute actions are needed:

- `<video>.muted = true` alone isn't enough because Chromium's
  PipeWire node stays connected to the sink and emits decoded
  samples that compete with mpv in the mixer
- `pactl set-sink-input-mute` alone isn't enough because future
  videos load with their own audio that competes until the next
  re-evaluation runs

### Chromium sink-input drift correction

The user-facing volume slider in the manager controls
`<video>.volume` in the kiosk renderer (software gain applied to
the HTML5 media element). PipeWire's per-stream sink-input volume
for Chromium is supposed to stay at 100% so the renderer's
`<video>.volume` is the only attenuation in the chain. In
practice, the sink-input can drift below 100% from several
sources:

- PipeWire's `module-stream-restore` remembering an older session
  where the stream was at a lower level
- Media keys (volume-down) forwarded over VNC, which most
  compositors deliver to the active sink-input rather than the sink
- Stray `pactl set-sink-input-volume` commands during debugging

When Chromium's sink-input drifts below 100%, every Pi is quieter
than nominal even though the manager slider says "100%". The
status heartbeat loop reasserts the Chromium sink-input back to
100% every ~30 seconds in kiosk mode (`_set_chromium_sink_input_volume`).
Webview mode skips the reassertion because there the sink-input
IS the operator's volume control.

To inspect the current state on a Pi:

```bash
sudo -u woozleton XDG_RUNTIME_DIR=/run/user/1000 \
  pactl list sink-inputs | grep -B 8 -A 1 'application.name = "Chromium"'
```

The reassertion logs at info level only when it actually changes
something - quiet during normal operation.

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

If the audio companion is silent while video plays:

```bash
# Is mpv's stream wired into the HDMI sink?
pw-link -l | grep -E "mpv|hdmi"
# Expected: mpv:output_FL/FR have |-> arrows pointing at
# alsa_output...hdmi.hdmi-stereo:playback_FL/FR

# Is mpv's stream muted at the PA layer?
pactl list sink-inputs | grep -B 2 -A 10 -i "mpv" | head -30
# Look for Volume:/Mute: lines

# Is pactl finding Chromium when override is on?
journalctl -u piframe-client -b | grep companion_chromium_mute
# Expected: count=1 muted=True (when override on),
#           count=1 muted=False (when override off / cleared)
```

## Audio Visualizer

When an audio playlist plays, the renderer overlays a Butterchurn
(WebGL port of MilkDrop) visualization on top of the otherwise-blank
`<video>` element. The vendored bundle ships with the client under
`vendor/butterchurn/`; `_write_html` copies the two minified scripts
to `/tmp/` next to the kiosk HTML and references them via `file://`.

### Curated preset list

`piframe_client.py` defines `AUDIO_VISUALIZER_PRESETS` - the source
of truth for which presets the renderer offers. The list is
advertised to the orchestrator in every heartbeat
(`status.visualizer_presets`) so the orchestrator can populate a
per-playlist visualizer dropdown without hard-coding the list itself.
Adding / removing a preset is a one-line change in this constant
plus a friendly-name entry in the orchestrator's
`visualizerDisplayName` map.

### Per-playlist pick

Audio playlists carry a `visualizer` field (string, default
`"random"`). The orchestrator forwards this on the `audio_playlist`
WebSocket command; the Pi reads it and applies one of:
- `"none"` - visualizer overlay suppressed entirely
- `"random"` - random pick + 5s cycle (legacy behavior)
- `"<preset name>"` - lock to that preset, no cycling

The renderer's `audioVis.applyChoice()` is also called on every
state-poll tick, so changing the dropdown mid-playback flips the
visual within ~250ms without dropping audio.

### Reactivity (silent parallel decode)

The visualizer needs FFT data, but `createMediaElementSource()` on
the audible `<video>` would seize its audio output. So the renderer
spins up a second silent `<audio>` element decoding the same source
and routes it through an `AnalyserNode` purely for FFT samples. The
silent element does NOT have `muted=true` / `volume=0` set -
Chromium's `MediaElementAudioSourceNode` short-circuits decode on
muted elements, leaving the analyser reading all zeros. The element
stays inaudible because we never connect the analyser to
`audioCtx.destination`. Drift between the audible / analyser streams
is corrected every 1.5s via a sync timer.

### Pause/stop sync

`applyControl("pause")` calls `audioVis.setPaused(true/false)` so
the analyser tracks the audible video's state. Without this the
visualizer keeps reacting while the music is paused. `showIdle()`
calls `audioVis.stop()` when the playlist ends with no idle media
(otherwise the visualizer kept painting indefinitely).

### Performance knobs

Two constants in `browser_renderer_template.py` control the GPU
budget: `RENDER_SCALE` (canvas size as fraction of viewport;
default 0.4 = 16% of native shader work) and `VIZ_MESH_SIZE`
(Butterchurn warp/comp grid; default 24, vs Butterchurn's default
of 48 = 25% of default vertex work).

Chromium's vsync is disabled (`--disable-gpu-vsync` +
`--disable-frame-rate-limit`) since cage+wayland's compositor caps
real fps at ~40 with vsync on, masking the actual render budget.
With both off, light presets reach 60+ and heavier ones drop
honestly to whatever they can sustain.

The init path probes WebGL via `WEBGL_debug_renderer_info` and logs
the renderer string under `audio_visualizer_status stage=webgl_renderer`
so it's verifiable that the V3D hardware path is active (vs a
SwiftShader software fallback). Per-preset FPS is sampled over a
4-second window and logged under `stage=preset_fps name=<...>
fps=<X>` for ongoing curation.

### Track-name OSD

When a new audio item starts, a glassy bottom-anchored pill shows
the prettified filename (extension dropped, underscores -> spaces,
leading "01. " / "12 - " track-number prefixes stripped). Auto-
hides after ~5s. Re-triggers on playlist advance / next press.

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
- `requirements.txt`
- `scripts/bootstrap_pi.sh`
- `scripts/test_webview.sh`
- `vendor/butterchurn/` (the audio visualizer bundle)
- `vendor/h264ify/` (the AV1-disable extension for webview mode)
- `idle.jpg`, `idle.html`
- `README.md`

Ignored:

- `api-env/` (local Python venv)
- `client_settings.json` (per-device runtime state)
- `__pycache__/` and Python bytecode
- local `*.log` files

## On-Screen Status Banner

The browser renderer can show a top-of-screen banner for important runtime issues without taking over the entire display.

Current banner cases include:

- `NAS unavailable`
- `Media file missing`
- `Website unavailable`
- `Server disconnected`

Behavior:

- the banner overlays current content
- it clears automatically when valid content resumes
- it is sized for the post-rotation viewport (the compositor rotates
  the framebuffer; the renderer treats the canvas as native portrait
  on a portrait-mounted Pi)

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
- `PIFRAME_WLR_RANDR_BIN` (default `wlr-randr`) - used to apply
  `PIFRAME_OUTPUT_TRANSFORM` to the cage output at startup
- `PIFRAME_OUTPUT_TRANSFORM` (default `90`) - cage output rotation,
  applied via `wlr-randr --transform`. Accepts `normal`, `90`,
  `180`, `270`, `flipped`, `flipped-90`, `flipped-180`,
  `flipped-270`. The bootstrap's `--orientation` flag covers the
  four common orientations (landscape / portrait / portrait-ccw /
  upside-down); the `flipped*` values are only reachable by setting
  this env var directly. See [Remote Control](#remote-control-vnc).
- `PIFRAME_OUTPUT_MODE` (default empty) - optional cage framebuffer
  mode, applied via `wlr-randr --mode` alongside the transform.
  Empty = honor the TV's EDID-native mode (typical). Set to a
  `WIDTHxHEIGHT` or `WIDTHxHEIGHT@RATE` string to override - e.g.
  `1920x1080@60` on 4K TVs so the kiosk + Butterchurn visualizer
  composite at 1/4 the pixels (the TV's scaler upsamples to native
  with no perceptible loss at typical viewing distance). The
  rotation watcher reapplies BOTH the transform and the mode on
  drift (after TV suspend/resume) so the framebuffer can't land in
  a half-fixed state. Bootstrap's `--output-mode` flag covers the
  common case; setting this env var directly works for unusual
  modes the TV's EDID still exposes.
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
- `PIFRAME_BROWSER_EVENT_PORT` (default `18888`) - loopback port for
  the kiosk JS to POST slide-change / pause events to (so the
  manager sees them in real time), and for the local control
  endpoint at `POST /control` (see [Remote Control](#remote-control-vnc))

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
