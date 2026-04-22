# PiFrame Client

`piframe_client.py` is the client that connects to the PiFrame server over WebSocket and renders all media through a single Chromium kiosk session running under `cage`.

This client now uses one browser-based renderer for:

- single videos
- video playlists
- image slideshows
- mixed image/video playlists
- idle fallback media

That browser-first approach replaced the older `mpv`-based path because it gave better behavior on this Raspberry Pi 5 setup:

- smoother image timing
- better mixed-media support
- cleaner transitions
- easier visual styling
- low enough CPU and memory usage to be practical in production

## Files

- [piframe_client.py](/home/woozleton/piframe_client/piframe_client.py)
- [idle.jpg](/home/woozleton/piframe_client/idle.jpg)
- [piframe-client.service](/etc/systemd/system/piframe-client.service)

## How It Works

At startup, the client:

1. connects to the server over WebSocket
2. starts a single Chromium kiosk session under `cage`
3. writes a local HTML renderer to `/tmp/piframe_browser.html`
4. writes browser state to `/tmp/piframe_browser_state.json`
5. updates that state whenever the server sends a playback command

The browser polls the state file and renders media fullscreen on the attached display.

## Supported Commands

The client currently handles these server-side commands:

- `play`
- `video_playlist`
- `slideshow`
- `pause`
- `next`
- `previous`
- `stop`
- `volume`

Single-video note:

- the server may normalize a bare `play` request into a one-item `video_playlist`
- the client detects that one-item video playlist case and renders it locally as a true single video

## Display Features

Current browser renderer features include:

- 270-degree rotation for portrait-mounted displays
- crossfade-style transitions using double-buffered stages
- mixed-media playlist support
- hidden cursor
- idle fallback image when nothing is playing
- top-of-screen rotated status banner for runtime issues

## Media Guidance

For this Raspberry Pi 5 browser renderer, the practical house format is:

- `1080p`
- `H.264`
- moderate bitrate
- muted or unnecessary audio removed when possible

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
Requires=network-online.target mnt-nas.mount
Wants=network-online.target

[Service]
User=woozleton
WorkingDirectory=/home/woozleton/piframe_client
ExecStart=/home/woozleton/piframe_client/api-env/bin/python /home/woozleton/piframe_client/piframe_client.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/1000

[Install]
WantedBy=multi-user.target
```

Useful commands:

```bash
sudo systemctl restart piframe-client
sudo systemctl status piframe-client --no-pager
journalctl -u piframe-client -f
```

## Logging

The client writes structured operational logs to `journalctl`.

Typical events include:

- `client_starting`
- `registered`
- `play_command`
- `video_playlist_command`
- `slideshow_command`
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

## Git / GitHub

This project folder is intended to be self-contained for source control:

- source lives in `/home/woozleton/piframe_client`
- the local Python runtime also lives in this folder as `api-env/`
- `api-env/` should not be committed

Recommended tracked files:

- `piframe_client.py`
- `piframe_client_backup.py`
- `README.md`
- `requirements.md`
- `idle.jpg`

Current ignore targets:

- `api-env/`
- `__pycache__/`
- Python bytecode and local log files

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

## Important Environment Variables

These can be set in the service file or shell environment.

### Core

- `PIFRAME_SERVER`
- `PIFRAME_NAS_ROOT`
- `PIFRAME_IDLE_MEDIA`
- `PIFRAME_CHROMIUM_BIN`
- `PIFRAME_CAGE_BIN`

### Browser Renderer

- `PIFRAME_BROWSER_SHOW_HUD`
- `PIFRAME_BROWSER_TRANSITION`
- `PIFRAME_BROWSER_TRANSITION_DURATION_MS`
- `PIFRAME_BROWSER_VIDEO_FILL_MODE`
- `PIFRAME_BROWSER_LOG_MAX_BYTES`
- `PIFRAME_BROWSER_LOG_BACKUPS`

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

4. restart the service:

```bash
sudo systemctl restart piframe-client
```

## Current Status

This client is now using the browser renderer as the single playback method for all supported media types. That is the intended architecture going forward.
