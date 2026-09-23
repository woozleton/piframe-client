#!/bin/sh
# Point ALSA's default device at whichever Pi 5 HDMI port has the TV.
#
# Runs as ExecStartPre of piframe-client (as the service user), so the
# answer follows the cable at every kiosk start instead of being baked
# in at bootstrap time - the same SD image then works whichever port a
# frame's TV is plugged into. PIFRAME_ALSA_DEVICE (bootstrap
# --alsa-device) pins a device and skips detection.
#
# Pi 5 mapping: card1-HDMI-A-1 -> ALSA card 0 (vc4hdmi0),
#               card1-HDMI-A-2 -> ALSA card 1 (vc4hdmi1).
# Only rewrites ~/.asoundrc when the content would change.

dev="${PIFRAME_ALSA_DEVICE:-}"
if [ -z "$dev" ]; then
  dev="plughw:0,0"
  if [ "$(cat /sys/class/drm/card1-HDMI-A-1/status 2>/dev/null)" != "connected" ] &&
     [ "$(cat /sys/class/drm/card1-HDMI-A-2/status 2>/dev/null)" = "connected" ]; then
    dev="plughw:1,0"
  fi
fi
card="${dev#*:}"
card="${card%%,*}"

target="${HOME:?HOME not set}/.asoundrc"
new="$(printf 'pcm.!default {\n  type plug\n  slave.pcm "%s"\n}\n\nctl.!default {\n  type hw\n  card %s\n}' "$dev" "$card")"
if [ -f "$target" ] && [ "$(cat "$target")" = "$new" ]; then
  exit 0
fi
printf '%s\n' "$new" > "$target.tmp" && mv "$target.tmp" "$target" && chmod 0644 "$target"
echo "asoundrc: default ALSA device -> $dev"
