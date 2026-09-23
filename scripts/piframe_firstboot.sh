#!/bin/bash
# piframe-firstboot: give a cloned SD card its own identity.
#
# Installed by bootstrap as /usr/local/sbin/piframe-firstboot and run by
# piframe-firstboot.service early in boot, before NetworkManager, ssh
# and the piframe units. It acts only when there is something to do:
#
#   /etc/piframe/firstboot-pending   (written by scripts/prepare_image.sh)
#       -> new /etc/machine-id, new SSH host keys, and a hostname: the
#          one in the file below, else frame-<last 6 of the Wi-Fi MAC>.
#   /boot/firmware/piframe-hostname.txt   (you create it on the SD card's
#       FAT partition, e.g. from Windows; one line, like "frame-hall")
#       -> set that hostname (works on its own too, to rename a frame).
#
# If anything changed it reboots once, so everything starts under the
# new identity. The frame's hostname IS its identity: the manager's
# client id, the Home Assistant device name and the CEC OSD name all
# come from it.
#
# Deliberately NOT systemd's first-boot mode (deleting /etc/machine-id):
# on Raspberry Pi OS that also runs systemd-firstboot --prompt-*, which
# can sit on the console waiting for a keyboard - a "hung" frame.

set -u
PENDING=/etc/piframe/firstboot-pending
NAMEFILE=/boot/firmware/piframe-hostname.txt
log() { echo "piframe-firstboot: $*"; }
changed=0

want=""
if [ -f "$NAMEFILE" ]; then
  # Tolerate Windows editors: BOM, CRLF, stray spaces, upper case.
  want="$(sed '1s/^\xEF\xBB\xBF//' "$NAMEFILE" | tr -d '\r\t ' | head -1 | tr 'A-Z' 'a-z')"
  rm -f "$NAMEFILE"
  if ! [[ "$want" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    log "ignoring invalid hostname '$want' from $NAMEFILE"
    want=""
  fi
fi

if [ -f "$PENDING" ]; then
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
  if [ -z "$want" ]; then
    mac="$(tr -d ':' < /sys/class/net/wlan0/address 2>/dev/null)"
    want="frame-${mac: -6}"
    [ "$want" = "frame-" ] && want="frame-${new_id:0:6}"
    log "no $NAMEFILE - using $want"
  fi
  rm -f "$PENDING"
  changed=1
fi

current="$(cat /etc/hostname 2>/dev/null)"
if [ -n "$want" ] && [ "$want" != "$current" ]; then
  log "hostname $current -> $want"
  echo "$want" > /etc/hostname
  if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$want/" /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$want" >> /etc/hosts
  fi
  changed=1
fi

sync
if [ "$changed" -eq 1 ]; then
  log "rebooting into the new identity"
  systemctl --no-block reboot
  # Hold the units ordered after us (network, ssh, kiosk) until the
  # reboot takes over, so nothing starts under the old identity.
  sleep 300
fi
