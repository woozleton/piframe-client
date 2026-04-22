# PiFrame Client Requirements

This file captures the practical requirements for running the current Chromium-based PiFrame client on additional Raspberry Pis.

## Hardware

- Raspberry Pi 5
- 4 GB RAM or better recommended
- portrait-mounted display / frame TV supported
- network access to the PiFrame server
- NAS mount available locally on the Pi

## Operating Model

The client is now browser-only for rendering:

- images
- videos
- image playlists
- video playlists
- mixed playlists
- idle fallback image

Rendering is handled by:

- `chromium`
- `cage`

The client runs as a `systemd` service and connects to the PiFrame manager over WebSocket.

## Software Requirements

Required packages / components:

- Python 3
- `chromium`
- `cage`
- a working `systemd` environment
- a valid `XDG_RUNTIME_DIR` for the service user

Python dependencies used by the client:

- `websocket-client`
- `psutil` (optional but recommended for status metrics)

## Files Required On Each Pi

Required project files:

- `piframe_client.py`
- `README.md`
- `requirements.md`
- `idle.jpg`

Optional to keep:

- `piframe_client_backup.py`
- `test/test.py`

## Service Requirements

Expected service file location:

- `/etc/systemd/system/piframe-client.service`

Required service behavior:

- runs as the non-root display user
- restarts automatically
- starts after networking and NAS mount are available

Current known-good service shape:

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

Adjust the following per Pi as needed:

- `User=...`
- `WorkingDirectory=...`
- `ExecStart=...`
- `XDG_RUNTIME_DIR=/run/user/<uid>`
- NAS mount unit name if not `mnt-nas.mount`

Recommended project layout:

- keep the Python runtime inside the client folder
- current path: `/home/woozleton/piframe_client/api-env`

## Environment Variables

Core:

- `PIFRAME_SERVER`
- `PIFRAME_NAS_ROOT`
- `PIFRAME_IDLE_MEDIA`
- `PIFRAME_CHROMIUM_BIN`
- `PIFRAME_CAGE_BIN`

Browser renderer:

- `PIFRAME_BROWSER_SHOW_HUD`
- `PIFRAME_BROWSER_TRANSITION`
- `PIFRAME_BROWSER_TRANSITION_DURATION_MS`
- `PIFRAME_BROWSER_VIDEO_FILL_MODE`
- `PIFRAME_BROWSER_LOG_MAX_BYTES`
- `PIFRAME_BROWSER_LOG_BACKUPS`

Current recommended default:

- `PIFRAME_BROWSER_VIDEO_FILL_MODE=contain` if you choose to set it explicitly

Notes:

- `blurred_fill` was explored, but the stable path right now is `contain`
- `cover` is available if you intentionally want cropping

## NAS / Media Assumptions

- media paths are normalized to a local NAS mount
- default mount root is `/mnt/nas`
- the client expects media to be reachable locally from the Pi

## Runtime Artifacts

The client will create runtime artifacts in `/tmp`, including:

- `/tmp/piframe_browser.html`
- `/tmp/piframe_browser_state.json`
- `/tmp/piframe_browser.log`
- `/tmp/piframe_chromium_cache`
- `/tmp/piframe_chromium_profile`

These do not need to be copied between devices.

## On-Screen Status Behavior

The Chromium renderer includes a rotated top banner for runtime issues.

Current banner messages:

- `NAS unavailable`
- `Media file missing`
- `Website unavailable`
- `Server disconnected`

This is useful on unattended displays because failures no longer look like a silent black screen.

## Logging / Debugging

Primary service logs:

```bash
journalctl -u piframe-client -f
```

Browser-side log:

```bash
tail -f /tmp/piframe_browser.log
```

## Performance Guidance

Recommended media profile for this hardware:

- `1080p`
- `H.264`
- moderate bitrate

Known limitation:

- `4K` video is not a good fit for the Chromium kiosk path on this Pi and can saturate CPU

## Functional Expectations

Known-good behaviors:

- image slideshows
- video playlists
- mixed playlists
- idle fallback
- one-item server-normalized video playlists routed locally as true single-video playback

## Deployment Checklist

For each new Pi:

1. install `chromium`
2. install `cage`
3. install Python dependencies
4. copy the `piframe_client` folder
5. place the service file in `/etc/systemd/system/piframe-client.service`
6. set correct user/path/env values
7. ensure NAS mount exists
8. run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable piframe-client
sudo systemctl restart piframe-client
```

9. verify:

```bash
systemctl status piframe-client --no-pager
journalctl -u piframe-client -n 50 --no-pager
```
