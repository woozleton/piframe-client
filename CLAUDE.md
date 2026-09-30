# PiFrame Client - notes for Claude

The kiosk client on each Pi picture frame. It connects to the PiFrame Manager in
woozlescape (on SPUT-SERV) over WebSocket and plays everything through one Chromium
kiosk under `cage`. `README.md` is the full guide.

A frame runs what `origin/main` held when the manager's System tab last sent it
`update_self` (`update.sh`), so a push to `main` reaches the frames on their next update.

## App map
`docs/app-map.mmd` is the frames' map for WoozleTrack's Maps page: what runs where and
what talks to what. It follows `docs/app-map-format.md` in woozleton/woozletrack (read it
with `gh api repos/woozleton/woozletrack/contents/docs/app-map-format.md -H "Accept: application/vnd.github.raw"`).
- Change it in the same commit as anything that changes how a frame is put together: a
  process or unit on the Pi, a port, what it connects to (the manager, the NAS, MQTT,
  the TV, NTP), or the machine something runs on.
- Box ids never change: WoozleTrack hangs each box's live status on its id. woozlescape's
  map draws some of the same parts, and the ids they share (`piframe_client`,
  `frame_music`, `wayvnc`, `woozlescape`, `nas_media`, `mosquitto`) stay the same in both.
- No style lines: WoozleTrack rounds every box and frame when it draws.
- At the start of every session, and before each commit that touches
  `piframe_client.py`, `piframe_cec.py`, `browser_renderer_template.py`, `update.sh`,
  or `scripts/`, read the map against the code and fix what has drifted. Check that it
  still draws with Mermaid 11.
