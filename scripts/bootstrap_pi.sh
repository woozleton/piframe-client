#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_USER="${SUDO_USER:-${USER}}"
SERVER_URL="ws://192.168.100.100:8080/ws"
NAS_ROOT="/mnt/nas"
MOUNT_UNIT="mnt-nas.mount"
SERVICE_NAME="piframe-client"
VNC_SERVICE_NAME="piframe-vnc"
VNC_LISTEN_ADDRESS="0.0.0.0"
VNC_LISTEN_PORT="5900"
INSTALL_SYSTEM_PACKAGES=1
# Display orientation. Friendly name; mapped to a wlr-randr transform
# below. Empty here means "ask, or inherit from existing service file".
ORIENTATION=""
# Optional framebuffer-mode override (matches wlr-randr --mode). Empty
# means honor the TV's EDID-native mode. Set to e.g. "1920x1080" on
# 4K TVs to drop the Pi's output to 1080p and let the TV upscale -
# Chromium + Butterchurn run at 1/4 the pixels and the Pi 5's V3D
# core stays in budget; the TV's built-in scaler handles the upsample.
OUTPUT_MODE=""
# ALSA device for audio output. Pi 5 has two HDMI ports (0 and 1) with
# different audio characteristics. Auto-detected from connected display.
# Format: plughw:X,Y where X is card number, Y is device number.
ALSA_DEVICE=""

detect_alsa_device() {
  # Auto-detect which HDMI port has a display connected.
  # Checks /sys/class/drm/ to see which HDMI ports are physically connected,
  # then maps them to ALSA card numbers (vc4hdmi0, vc4hdmi1). Returns the
  # ALSA device for the first connected port found. Defaults to plughw:0,0
  # if no display is detected or detection fails.
  local hdmi_a1_status hdmi_a2_status card

  # Check physical HDMI port status via kernel DRM interface
  if [[ -f /sys/class/drm/card1-HDMI-A-1/status ]]; then
    hdmi_a1_status=$(cat /sys/class/drm/card1-HDMI-A-1/status 2>/dev/null || echo "unknown")
  fi
  if [[ -f /sys/class/drm/card1-HDMI-A-2/status ]]; then
    hdmi_a2_status=$(cat /sys/class/drm/card1-HDMI-A-2/status 2>/dev/null || echo "unknown")
  fi

  # Map DRM outputs to ALSA cards. On Pi 5:
  #   card1-HDMI-A-1 typically maps to vc4hdmi0 (ALSA card 0)
  #   card1-HDMI-A-2 typically maps to vc4hdmi1 (ALSA card 1)
  # Try them in order and use the first connected one found.
  if [[ "${hdmi_a1_status}" == "connected" ]]; then
    echo "plughw:0,0"
    return
  fi

  if [[ "${hdmi_a2_status}" == "connected" ]]; then
    echo "plughw:1,0"
    return
  fi

  # Fallback to port 0 if no display detected or detection failed
  echo "plughw:0,0"
}

usage() {
  cat <<EOF
Usage: sudo ./scripts/bootstrap_pi.sh [options]

Options:
  --user <name>          Service user. Default: ${SERVICE_USER}
  --server <url>         PiFrame Manager websocket URL.
                         Default: ${SERVER_URL}
  --nas-root <path>      NAS mount root. Default: ${NAS_ROOT}
  --mount-unit <unit>    systemd mount unit name. Default: ${MOUNT_UNIT}
  --orientation <name>   Display orientation. One of:
                           landscape       (TV mounted normally)
                           portrait        (TV rotated 90° clockwise from landscape)
                           portrait-ccw    (TV rotated 90° counter-clockwise)
                           upside-down     (TV rotated 180°)
                         If omitted, prompts interactively on first run
                         and reuses the existing setting on re-runs.
  --output-mode <mode>   Force framebuffer mode (wlr-randr --mode).
                         Examples: "1920x1080" or "1920x1080@60".
                         Empty/omitted = honor the TV's native EDID
                         mode. Useful on 4K TVs to drop output to
                         1080p; Chromium + visualizer composite at
                         1/4 the pixels and the TV upscales.
  --alsa-device <dev>    ALSA device for audio (plughw:card,device).
                         Auto-detected from connected display if omitted.
                         The Pi 5 has two HDMI ports: 0 and 1.
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
    --orientation)
      ORIENTATION="$2"
      shift 2
      ;;
    --output-mode)
      OUTPUT_MODE="$2"
      shift 2
      ;;
    --alsa-device)
      ALSA_DEVICE="$2"
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

# Auto-detect ALSA device if not explicitly provided
if [[ -z "${ALSA_DEVICE}" ]]; then
  ALSA_DEVICE=$(detect_alsa_device)
  echo "Auto-detected ALSA device: ${ALSA_DEVICE}"
fi

USER_UID="$(id -u "${SERVICE_USER}")"
USER_GID="$(id -g "${SERVICE_USER}")"
USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
VENV_DIR="${REPO_DIR}/api-env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ----------------------------------------------------------------
# Resolve display orientation -> wlr-randr transform value.
# Precedence:
#   1. --orientation flag (explicit, always wins)
#   2. existing service file with PIFRAME_OUTPUT_TRANSFORM line
#      (re-run on a configured Pi: keep that value)
#   3. existing service file without that line (re-run on a Pi
#      bootstrapped before this prompt was added: silent default
#      to portrait so we don't surprise the operator with a prompt
#      on a routine bootstrap re-run)
#   4. no service file yet AND stdin is a terminal (first-time
#      install on a fresh Pi): prompt interactively
#   5. no service file yet AND no terminal (unattended install):
#      silent default to portrait
# ----------------------------------------------------------------
orientation_to_transform() {
  case "$1" in
    landscape)    echo "normal" ;;
    portrait)     echo "90" ;;
    portrait-ccw) echo "270" ;;
    upside-down)  echo "180" ;;
    *) return 1 ;;
  esac
}

OUTPUT_TRANSFORM_VALUE=""
if [[ -n "${ORIENTATION}" ]]; then
  if ! OUTPUT_TRANSFORM_VALUE="$(orientation_to_transform "${ORIENTATION}")"; then
    echo "Unknown orientation: ${ORIENTATION}" >&2
    echo "Expected: landscape | portrait | portrait-ccw | upside-down" >&2
    exit 1
  fi
elif [[ -f "${SERVICE_FILE}" ]]; then
  EXISTING_TRANSFORM="$(grep -oE 'PIFRAME_OUTPUT_TRANSFORM=[a-z0-9-]+' "${SERVICE_FILE}" | head -1 | cut -d= -f2 || true)"
  if [[ -n "${EXISTING_TRANSFORM}" ]]; then
    OUTPUT_TRANSFORM_VALUE="${EXISTING_TRANSFORM}"
    echo "Reusing existing orientation (transform=${OUTPUT_TRANSFORM_VALUE}) from ${SERVICE_FILE}"
  else
    # Existing service file but no PIFRAME_OUTPUT_TRANSFORM line -
    # this is a Pi bootstrapped before the orientation prompt was
    # added. Inherit the historical default (portrait) silently
    # rather than prompting out of nowhere on a re-run.
    OUTPUT_TRANSFORM_VALUE="90"
    echo "Existing ${SERVICE_FILE} has no PIFRAME_OUTPUT_TRANSFORM; defaulting to portrait (transform=90). Pass --orientation to override."
  fi
fi
# Same inheritance pattern for OUTPUT_MODE: explicit --output-mode
# wins; otherwise re-runs inherit whatever is already baked into the
# service file. Empty string means "honor TV's native mode" (no
# wlr-randr --mode flag emitted at runtime); a value like
# "1920x1080" forces 1080p on 4K-capable TVs.
if [[ -z "${OUTPUT_MODE}" && -f "${SERVICE_FILE}" ]]; then
  EXISTING_MODE="$(grep -oE 'PIFRAME_OUTPUT_MODE=[A-Za-z0-9@x.-]+' "${SERVICE_FILE}" | head -1 | cut -d= -f2 || true)"
  if [[ -n "${EXISTING_MODE}" ]]; then
    OUTPUT_MODE="${EXISTING_MODE}"
    echo "Reusing existing output mode (${OUTPUT_MODE}) from ${SERVICE_FILE}"
  fi
fi

if [[ -z "${OUTPUT_TRANSFORM_VALUE}" ]]; then
  if [[ -t 0 ]]; then
    cat <<'EOF'

How is the TV physically mounted?
  1) landscape       (TV in its normal horizontal position)
  2) portrait        (TV rotated 90° clockwise from landscape)
  3) portrait-ccw    (TV rotated 90° counter-clockwise)
  4) upside-down     (TV rotated 180°)

EOF
    choice=""
    while true; do
      # If read fails (e.g. EOF / piped stdin closes), default to
      # portrait rather than looping forever. set -u makes the
      # post-read expansion below safe with this initialization.
      if ! read -r -p "Select [1-4, default 2 = portrait]: " choice; then
        OUTPUT_TRANSFORM_VALUE="90"
        echo
        echo "(no input; defaulting to portrait)"
        break
      fi
      choice="${choice:-2}"
      case "${choice}" in
        1|landscape)    OUTPUT_TRANSFORM_VALUE="normal"; break ;;
        2|portrait)     OUTPUT_TRANSFORM_VALUE="90"; break ;;
        3|portrait-ccw) OUTPUT_TRANSFORM_VALUE="270"; break ;;
        4|upside-down)  OUTPUT_TRANSFORM_VALUE="180"; break ;;
        *) echo "Invalid choice. Enter 1, 2, 3, or 4." ;;
      esac
    done
  else
    OUTPUT_TRANSFORM_VALUE="90"
    echo "No --orientation flag and no terminal; defaulting to portrait (transform=90)."
  fi
fi

VNC_SERVICE_FILE="/etc/systemd/system/${VNC_SERVICE_NAME}.service"
VNC_CONFIG_DIR="${USER_HOME}/.config/wayvnc"
VNC_CONFIG_FILE="${VNC_CONFIG_DIR}/config"
ORIGIN_URL="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"

if [[ ${INSTALL_SYSTEM_PACKAGES} -eq 1 ]]; then
  apt-get update
  apt-get install -y \
    chromium \
    cage \
    seatd \
    wlrctl \
    wlr-randr \
    wayvnc \
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

# The wayvnc package on Raspberry Pi OS ships its own systemd unit
# that runs as user `vnc` with a private XDG_RUNTIME_DIR. That's the
# wrong shape for us - we need wayvnc to attach to the cage Wayland
# session running under ${SERVICE_USER}, otherwise it captures an
# empty headless session instead of the kiosk.
systemctl disable --now wayvnc.service 2>/dev/null || true
systemctl mask wayvnc.service 2>/dev/null || true

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
# Output rotation applied to the cage compositor at kiosk start.
# normal=landscape, 90=portrait (default mount), 270=portrait-ccw,
# 180=upside-down. See README "Remote Control (VNC)" for why this
# is at the compositor instead of in CSS.
Environment=PIFRAME_OUTPUT_TRANSFORM=${OUTPUT_TRANSFORM_VALUE}
# Optional framebuffer mode override (matches wlr-randr --mode).
# Empty = honor the TV's EDID-native mode. "1920x1080" or
# "1920x1080@60" forces 1080p output on 4K TVs so Chromium +
# Butterchurn composite at 1/4 the pixels; the TV's built-in scaler
# upsamples to native.
Environment=PIFRAME_OUTPUT_MODE=${OUTPUT_MODE}

[Install]
WantedBy=multi-user.target
EOF

# Enable user lingering so /run/user/<uid> exists at boot without an
# interactive login. Cage + Chromium need that directory for the
# Wayland socket, and without lingering it only appears after the
# user logs in - so the service starts at multi-user.target before
# the runtime dir is created and chromium fails to open a window.
loginctl enable-linger "${SERVICE_USER}"

# Configure ALSA device. The Pi 5 has two HDMI ports (0 and 1) with
# different audio characteristics; port 0 produces better audio output.
# Default is plughw:0,0 (HDMI port 0) but can be overridden with --alsa-device.
cat > "${USER_HOME}/.asoundrc" <<ASOUNDRC
pcm.!default {
  type plug
  slave.pcm "${ALSA_DEVICE}"
}

ctl.!default {
  type hw
  card $(echo "${ALSA_DEVICE}" | cut -d: -f2 | cut -d, -f1)
}
ASOUNDRC
chown "${USER_UID}:${USER_GID}" "${USER_HOME}/.asoundrc"
chmod 0644 "${USER_HOME}/.asoundrc"

# Enable and start the PipeWire audio server for the service user.
# This is required for audio companion to work: Chromium locks the
# audio device while <video> plays (even when muted), which prevents
# the mpv audio sidecar from claiming HDMI. PipeWire adds a userspace
# mixer that lets both streams coexist. Without it, the audio
# companion stays inaudible while video plays (bare ALSA is single-stream).
sudo -u "${SERVICE_USER}" \
  XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
  systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || true

# Pi's default HDMI sink volume defaults vary (some boards come up at
# ~40%). Pin to 50% as a sane TV-friendly ceiling - 100% was uncomfor-
# tably loud on the displays we deploy to. Operators can still nudge
# higher per-device via `pactl set-sink-volume @DEFAULT_SINK@ <N>%`.
# Runs as the service user because sinks are per-user under PipeWire.
sudo -u "${SERVICE_USER}" \
  XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
  pactl set-sink-volume @DEFAULT_SINK@ 50% 2>/dev/null || true

# wayvnc config + system unit for remote control of the kiosk display.
# The unit attaches to the cage Wayland session owned by ${SERVICE_USER}
# so VNC viewers see the actual kiosk content (not a headless session).
# Auth is left disabled for v1 to match the existing LAN-trust posture
# documented in README.md - the port should not be exposed beyond the
# LAN. Enable wayvnc auth + TLS later by editing ${VNC_CONFIG_FILE}.
install -d -m 0755 -o "${USER_UID}" -g "${USER_GID}" "${VNC_CONFIG_DIR}"
if [[ ! -f "${VNC_CONFIG_FILE}" ]]; then
  cat > "${VNC_CONFIG_FILE}" <<EOF
address=${VNC_LISTEN_ADDRESS}
enable_auth=false
EOF
  chown "${USER_UID}:${USER_GID}" "${VNC_CONFIG_FILE}"
  chmod 0644 "${VNC_CONFIG_FILE}"
fi

cat > "${VNC_SERVICE_FILE}" <<EOF
[Unit]
Description=PiFrame VNC (wayvnc attached to the cage kiosk)
After=${SERVICE_NAME}.service
Wants=${SERVICE_NAME}.service
# Cage gets torn down whenever the client switches between kiosk and
# webview modes, which makes wayvnc lose its Wayland socket. Disable
# systemd's start-limit rate cap so wayvnc keeps retrying after the
# mode swap rather than giving up after the default ~5 fails / 10s.
StartLimitIntervalSec=0

[Service]
User=${SERVICE_USER}
# Match the cage session's runtime / wayland socket so wayvnc captures
# the kiosk output instead of starting a headless session.
Environment=XDG_RUNTIME_DIR=/run/user/${USER_UID}
Environment=WAYLAND_DISPLAY=wayland-0
ExecStart=/usr/bin/wayvnc --config=${VNC_CONFIG_FILE} ${VNC_LISTEN_ADDRESS} ${VNC_LISTEN_PORT}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

# ----------------------------------------------------------------
# Point systemd-timesyncd at the woozlescape server's LAN NTP.
# The mural's cross-screen sync needs the fleet to agree with the
# SERVER's clock (the show's clock master), not with true UTC -
# LAN sync gets offsets into the ~1ms range vs internet NTP's
# 5-50ms. Idempotent: rewrites any existing NTP= line (commented
# or not); override the server with PIFRAME_NTP_SERVER, or set it
# empty to skip.
# ----------------------------------------------------------------
NTP_SERVER="${PIFRAME_NTP_SERVER-192.168.100.100}"
if [[ -n "${NTP_SERVER}" && -f /etc/systemd/timesyncd.conf ]]; then
  if ! grep -qE '^\[Time\]' /etc/systemd/timesyncd.conf; then
    printf '\n[Time]\n' >> /etc/systemd/timesyncd.conf
  fi
  if grep -qE '^#?NTP=' /etc/systemd/timesyncd.conf; then
    sed -i "s|^#\?NTP=.*|NTP=${NTP_SERVER}|" /etc/systemd/timesyncd.conf
  else
    sed -i "s|^\[Time\]|[Time]\nNTP=${NTP_SERVER}|" /etc/systemd/timesyncd.conf
  fi
  systemctl restart systemd-timesyncd 2>/dev/null || true
  echo "NTP: systemd-timesyncd -> ${NTP_SERVER}"
fi

systemctl daemon-reload
systemctl enable --now seatd
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl enable "${VNC_SERVICE_NAME}"
systemctl restart "${VNC_SERVICE_NAME}"

cat <<EOF

PiFrame bootstrap complete.

Repo dir:        ${REPO_DIR}
Service user:    ${SERVICE_USER}
Service file:    ${SERVICE_FILE}
Server URL:      ${SERVER_URL}
NAS root:        ${NAS_ROOT}
Orientation:     transform=${OUTPUT_TRANSFORM_VALUE}
Audio device:    ${ALSA_DEVICE}
VNC service:     ${VNC_SERVICE_FILE} (listening on ${VNC_LISTEN_ADDRESS}:${VNC_LISTEN_PORT})
VNC config:      ${VNC_CONFIG_FILE}

Useful checks:
  systemctl status ${SERVICE_NAME} --no-pager
  journalctl -u ${SERVICE_NAME} -f
  systemctl status ${VNC_SERVICE_NAME} --no-pager
  pactl get-sink-volume @DEFAULT_SINK@

If audio does not work on the target Pi, verify the ALSA device with: aplay -l
EOF
