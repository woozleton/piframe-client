#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_USER="${SUDO_USER:-${USER}}"
SERVER_URL="ws://192.168.100.100:8080/ws"
NAS_ROOT="/mnt/nas"
MOUNT_UNIT="mnt-nas.mount"
SERVICE_NAME="piframe-client"
INSTALL_SYSTEM_PACKAGES=1

usage() {
  cat <<EOF
Usage: sudo ./scripts/bootstrap_pi.sh [options]

Options:
  --user <name>          Service user. Default: ${SERVICE_USER}
  --server <url>         PiFrame Manager websocket URL.
                         Default: ${SERVER_URL}
  --nas-root <path>      NAS mount root. Default: ${NAS_ROOT}
  --mount-unit <unit>    systemd mount unit name. Default: ${MOUNT_UNIT}
  --skip-apt             Skip apt package installation.
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      SERVICE_USER="$2"
      shift 2
      ;;
    --server)
      SERVER_URL="$2"
      shift 2
      ;;
    --nas-root)
      NAS_ROOT="$2"
      shift 2
      ;;
    --mount-unit)
      MOUNT_UNIT="$2"
      shift 2
      ;;
    --skip-apt)
      INSTALL_SYSTEM_PACKAGES=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this script with sudo." >&2
  exit 1
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User does not exist: ${SERVICE_USER}" >&2
  exit 1
fi

USER_UID="$(id -u "${SERVICE_USER}")"
USER_GID="$(id -g "${SERVICE_USER}")"
VENV_DIR="${REPO_DIR}/api-env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ORIGIN_URL="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"

if [[ ${INSTALL_SYSTEM_PACKAGES} -eq 1 ]]; then
  apt-get update
  apt-get install -y \
    chromium \
    cage \
    seatd \
    wlrctl \
    gh \
    python3 \
    python3-venv \
    python3-pip \
    alsa-utils \
    mpv \
    pipewire \
    pipewire-pulse \
    wireplumber \
    pulseaudio-utils
fi

if [[ -n "${ORIGIN_URL}" && "${ORIGIN_URL}" == https://github.com/* ]]; then
  if ! sudo -u "${SERVICE_USER}" gh auth status >/dev/null 2>&1; then
    cat <<EOF

GitHub authentication is not configured for ${SERVICE_USER}.

If this repo is private, authenticate first:
  sudo -u ${SERVICE_USER} gh auth login --hostname github.com --git-protocol https

Then confirm access:
  sudo -u ${SERVICE_USER} gh auth status

Continuing with local bootstrap because the repo is already present on disk.
EOF
  fi
fi

# Detect a broken venv and recreate it. Two ways the venv goes bad
# after an apt upgrade:
#   1. The interpreter symlink points at a Python binary that was
#      removed (e.g. /usr/bin/python3.11 after an upgrade to 3.13).
#   2. The pip shebang points at the old interpreter even when the
#      venv's own python still works - so `pip --version` errors with
#      "required file not found" while `python -c ...` succeeds.
# Probe both by running pip itself.
if [[ -d "${VENV_DIR}" ]] && ! "${VENV_DIR}/bin/pip" --version >/dev/null 2>&1; then
  echo "Existing venv is broken (pip cannot execute). Recreating: ${VENV_DIR}"
  rm -rf "${VENV_DIR}"
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"

chown -R "${USER_UID}:${USER_GID}" "${VENV_DIR}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=PiFrame Client
# Soft dependencies (Wants/After only) so a flaky network or NAS
# mount at boot doesn't block the service indefinitely. The client
# itself reconnects to the WS server on its own retry loop and
# tolerates a briefly-unavailable NAS - hard-requiring those units
# meant any boot-time failure put the service in failed state and
# kept it down until a manual systemctl start.
After=network-online.target ${MOUNT_UNIT}
Wants=network-online.target ${MOUNT_UNIT}

[Service]
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/python ${REPO_DIR}/piframe_client.py
# Keep restarting on any failure so a transient boot-order race
# (XDG_RUNTIME_DIR not yet created by logind, NAS mount slow, etc)
# resolves itself within one or two retries.
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/${USER_UID}
Environment=PIFRAME_SERVER=${SERVER_URL}
Environment=PIFRAME_NAS_ROOT=${NAS_ROOT}

[Install]
WantedBy=multi-user.target
EOF

# Enable user lingering so /run/user/<uid> exists at boot without an
# interactive login. Cage + Chromium need that directory for the
# Wayland socket, and without lingering it only appears after the
# user logs in - so the service starts at multi-user.target before
# the runtime dir is created and chromium fails to open a window.
loginctl enable-linger "${SERVICE_USER}"

# Pi's default HDMI sink volume defaults vary (some boards come up at
# ~40%). Force the user-default sink to 100% so the OVR mute path and
# companion playback are audible without per-Pi tweaks. Runs as the
# service user because sinks are per-user under PipeWire.
sudo -u "${SERVICE_USER}" \
  XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
  pactl set-sink-volume @DEFAULT_SINK@ 100% 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now seatd
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

cat <<EOF

PiFrame bootstrap complete.

Repo dir:        ${REPO_DIR}
Service user:    ${SERVICE_USER}
Service file:    ${SERVICE_FILE}
Server URL:      ${SERVER_URL}
NAS root:        ${NAS_ROOT}

Useful checks:
  systemctl status ${SERVICE_NAME} --no-pager
  journalctl -u ${SERVICE_NAME} -f

If audio does not work on the target Pi, verify the ALSA default device for that TV.
EOF
