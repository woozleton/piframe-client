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
- `webview_open` (optional `url` field; see [Remote Control](#remote-control-vnc))
- `webview_close`

Single-video note:

- the server may normalize a bare `play` request into a one-item `video_playlist`
- the client detects that one-item video playlist case and renders it locally as a true single video

Audio companion note:

- when `audio_playlist` arrives with `is_companion: true`, the client routes the audio to a sidecar `mpv` process instead of replacing the visual content
- the visual `<video>` is muted (when `mute_visual: true`) so the companion is the only audio source the operator hears
- see [Audio](#audio) for the OS-level audio mixer requirement

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

### Testing webview mode without the manager

`scripts/test_webview.sh` drives `BrowserController.set_browser_mode()`
directly so you can exercise webview mode from an SSH session before
the manager is wired up to send `webview_open` / `webview_close`.

```bash
sudo ./scripts/test_webview.sh                       # opens about:blank
sudo ./scripts/test_webview.sh https://youtube.com   # opens that URL
```

What it does:

- stops `piframe-client` (the device shows offline in the manager)
- spins up cage + a windowed Chromium pointed at the URL (or
  `about:blank`); VNC into `<pi-ip>:5900` to use the address bar
- on Ctrl-C tears down the webview and restarts `piframe-client`
  so the device returns to normal manager-driven operation

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
- `PIFRAME_WLR_RANDR_BIN` (default `wlr-randr`) - used to apply
  `PIFRAME_OUTPUT_TRANSFORM` to the cage output at startup
- `PIFRAME_OUTPUT_TRANSFORM` (default `90`) - cage output rotation,
  applied via `wlr-randr --transform`. Accepts `normal`, `90`,
  `180`, `270`, `flipped`, `flipped-90`, `flipped-180`,
  `flipped-270`. See [Remote Control](#remote-control-vnc).
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
