#!/bin/bash
# piframe-netwatch: bring the frame back onto the network by itself,
# without rebooting.
#
# Installed by bootstrap as /usr/local/sbin/piframe-netwatch and run
# every minute by piframe-netwatch.timer. "Online" means the default
# gateway answers a ping - Wi-Fi reporting "connected" isn't enough.
#
# On 2026-09-23 an access point dropped and four frames never came back
# after it returned (the ones that could see a second AP roamed within
# seconds). NetworkManager gives up after its default 4 attempts, and the
# Pi's brcmfmac Wi-Fi driver is known to wedge after an AP vanishes.
# The Wi-Fi profile now retries forever (bootstrap sets
# autoconnect-retries=0); this is the backstop for everything else.
#
# While offline for 5+ minutes it alternates two fixes, waiting 5, 5,
# 10, 20, then 30 minutes (max) between attempts:
#   odd attempts : Wi-Fi radio off/on (NetworkManager re-runs the
#                  connection)
#   even attempts: reload the Wi-Fi driver (brcmfmac) and restart
#                  wpa_supplicant + NetworkManager (safe: the NAS is a
#                  mount unit, so a NetworkManager restart no longer
#                  stalls systemd - see README "OS packages")
# The kiosk keeps running throughout. Every action and every recovery
# is logged to /var/lib/piframe-netwatch/log (persistent, last 500
# lines) so the next outage shows which step brought it back.

set -u
RUN=/run/piframe-netwatch
LOG_DIR=/var/lib/piframe-netwatch
LOG="$LOG_DIR/log"
mkdir -p "$RUN" "$LOG_DIR"
now=$(date +%s)

log() {
  echo "$(date -Is) $*" >> "$LOG"
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
}

online() {
  local gw
  gw="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
  [ -n "$gw" ] && ping -c 1 -W 3 "$gw" >/dev/null 2>&1
}

if online; then
  if [ -f "$RUN/down-since" ]; then
    since=$(cat "$RUN/down-since")
    log "back online after $(( now - since ))s (attempts: $(cat "$RUN/attempts" 2>/dev/null || echo 0); last: $(cat "$RUN/last-kind" 2>/dev/null || echo none))"
    rm -f "$RUN/down-since" "$RUN/attempts" "$RUN/last-action" "$RUN/last-kind"
  fi
  exit 0
fi

if [ ! -f "$RUN/down-since" ]; then
  echo "$now" > "$RUN/down-since"
  log "offline (no answer from the default gateway)"
  exit 0
fi

since=$(cat "$RUN/down-since")
attempts=$(cat "$RUN/attempts" 2>/dev/null || echo 0)
last=$(cat "$RUN/last-action" 2>/dev/null || echo "$since")
case "$attempts" in
  0|1) wait=300 ;;
  2) wait=600 ;;
  3) wait=1200 ;;
  *) wait=1800 ;;
esac
[ $(( now - last )) -ge "$wait" ] || exit 0

attempts=$(( attempts + 1 ))
echo "$attempts" > "$RUN/attempts"
echo "$now" > "$RUN/last-action"
if [ $(( attempts % 2 )) -eq 1 ]; then
  echo "radio" > "$RUN/last-kind"
  log "attempt $attempts after $(( now - since ))s offline: Wi-Fi radio off/on"
  nmcli radio wifi off
  sleep 3
  nmcli radio wifi on
else
  echo "driver" > "$RUN/last-kind"
  log "attempt $attempts after $(( now - since ))s offline: reload brcmfmac + restart wpa_supplicant/NetworkManager"
  systemctl stop NetworkManager wpa_supplicant
  modprobe -r brcmfmac_wcc brcmfmac 2>>"$LOG"
  sleep 2
  modprobe brcmfmac 2>>"$LOG"
  sleep 3
  systemctl start wpa_supplicant NetworkManager
fi
