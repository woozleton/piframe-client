#!/bin/bash
# Prepare this frame's SD card to be imaged as the template for new
# frames. Run on a working, bootstrapped frame, then image the card.
#
#   sudo ./scripts/prepare_image.sh          # asks first, powers off
#   sudo ./scripts/prepare_image.sh --yes    # no question
#
# What it does:
#   - stops the kiosk, VNC and CEC units
#   - marks the card for piframe-firstboot: on its next boot (this card
#     or any copy) it gets a new machine-id, new SSH host keys and a
#     hostname from piframe-hostname.txt on the FAT partition (else
#     frame-<MAC suffix>), then reboots once
#   - clears per-frame state: CEC desired state, client volume/mute,
#     shell history, apt cache
#   - syncs and powers off
#
# Kept on purpose (identical on every frame): Wi-Fi keyfile, NAS
# credentials + mount unit, MQTT login, SSH authorized_keys, the
# piframe-client checkout and venv.
#
# IMPORTANT: put piframe-hostname.txt on THIS card too before it boots
# again, or it renames itself to frame-<MAC suffix>. See README
# "Cloning a frame".

set -euo pipefail
[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
[[ -x /usr/local/sbin/piframe-firstboot ]] || {
  echo "piframe-firstboot is not installed - run scripts/bootstrap_pi.sh first." >&2; exit 1; }

SERVICE_USER="${SUDO_USER:-woozleton}"
USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" != "--yes" ]]; then
  echo "This stops the kiosk on $(hostname), marks the card for re-identification"
  echo "on its next boot, and POWERS OFF. Image the SD card after that."
  read -r -p "Continue? [y/N] " answer
  [[ "${answer}" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

systemctl stop piframe-client piframe-vnc piframe-cec 2>/dev/null || true

install -d -m 0755 /etc/piframe
echo "prepared $(date -Is) from $(hostname)" > /etc/piframe/firstboot-pending

rm -f /var/lib/piframe-cec/state.json
rm -f "${REPO_DIR}/client_settings.json"
rm -f "${USER_HOME}/.bash_history" /root/.bash_history
apt-get clean
git -C "${REPO_DIR}" status --porcelain | grep -q . && \
  echo "note: ${REPO_DIR} has local changes; self-update will replace them on the copies."

sync
echo "Prepared. Powering off - image the SD card now."
systemctl poweroff
