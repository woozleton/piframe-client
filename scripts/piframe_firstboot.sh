#!/bin/bash
# piframe-firstboot: give a card its own identity when it boots in a
# Raspberry Pi it doesn't belong to (a cloned card, or a moved one).
#
# Installed by bootstrap as /usr/local/sbin/piframe-firstboot and run by
# piframe-firstboot.service on EVERY boot, early - before
# NetworkManager, ssh and the piframe units. It compares this Pi's
# board serial with the one recorded in /etc/piframe/owner-serial:
#
#   same board      -> nothing to do (every normal boot)
#   no record yet   -> record this board and carry on (adopt)
#   different board -> new /etc/machine-id, new SSH host keys, the
#                      board's hostname from frames.conf (else
#                      piclient-<last 6 of the Wi-Fi MAC>), forget the
#                      previous frame's CEC screen state, record the
#                      board, reboot once
#
# So cloning is just: image any frame's card, write it, boot it. The
# hostname IS the frame's identity: the manager's client id, the Home
# Assistant device and the CEC OSD name all come from it.
#
# Deliberately NOT systemd's first-boot mode (an empty /etc/machine-id):
# on Raspberry Pi OS that also runs systemd-firstboot --prompt-*, which
# can sit on the console waiting for a keyboard - a "hung" frame.

set -u
RECORD=/etc/piframe/owner-serial
FRAMES_CONF="${PIFRAME_FRAMES_CONF:-/home/woozleton/piframe_client/frames.conf}"
log() { echo "piframe-firstboot: $*"; }

serial="$(tr -d '\0' < /sys/firmware/devicetree/base/serial-number 2>/dev/null)"
if [ -z "$serial" ]; then
  log "no board serial readable - nothing to do"
  exit 0
fi
recorded="$(cat "$RECORD" 2>/dev/null)"

if [ "$recorded" = "$serial" ]; then
  exit 0
fi
install -d -m 0755 /etc/piframe
if [ -z "$recorded" ]; then
  echo "$serial" > "$RECORD"
  log "recorded this board ($serial) as the card's owner"
  exit 0
fi

log "card last ran on board $recorded, this is $serial - new identity"

old_id="$(cat /etc/machine-id 2>/dev/null)"
new_id="$(systemd-id128 new)"
if [[ "$new_id" =~ ^[0-9a-f]{32}$ ]] && [ "$new_id" != "$old_id" ]; then
  echo "$new_id" > /etc/machine-id
  chmod 0444 /etc/machine-id
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
  log "machine-id ${old_id:0:8}... -> ${new_id:0:8}..."
else
  log "WARNING: could not generate a new machine-id; keeping the old one"
fi

rm -f /etc/ssh/ssh_host_*
ssh-keygen -A
log "new SSH host keys"

name="$(awk -v s="$serial" '!/^[[:space:]]*#/ && $1 == s { print $2; exit }' "$FRAMES_CONF" 2>/dev/null)"
if ! [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  mac="$(tr -d ':' < /sys/class/net/wlan0/address 2>/dev/null)"
  name="piclient-${mac: -6}"
  [ "$name" = "piclient-" ] && name="piclient-${serial: -6}"
fi
log "hostname $(cat /etc/hostname 2>/dev/null) -> $name"
echo "$name" > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$name/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$name" >> /etc/hosts
fi

# The previous frame's desired screen state (Home Assistant's last
# on/off) doesn't apply to this one; the service defaults to on.
rm -f /var/lib/piframe-cec/state.json

echo "$serial" > "$RECORD"
sync
log "rebooting into the new identity"
systemctl --no-block reboot
# Hold the units ordered after us (network, ssh, kiosk) until the
# reboot takes over, so nothing starts under the old identity.
sleep 300
