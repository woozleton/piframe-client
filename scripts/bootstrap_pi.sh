#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_USER="${SUDO_USER:-${USER}}"
SERVER_URL="ws://192.168.100.100:8080/ws"
NAS_ROOT="/mnt/nas"
MOUNT_UNIT="mnt-nas.mount"
# What the fleet's fstab points at. Only used by the NAS mount check
# below to flag a frame whose fstab drifted (the share folder was
# renamed once and one frame kept the old name -> mount failed ->
# black screen while the client still reported "slideshow").
NAS_SHARE_EXPECTED="//192.168.100.15/Media/Displays"
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
# TV power over HDMI-CEC (piframe_cec.py): its own unit, talking MQTT to
# Home Assistant's Mosquitto broker. Settings live in a root-only env file
# so the password never lands in a world-readable unit file. Empty values
# here mean "inherit from the existing env file, else default" (password:
# else PIFRAME_MQTT_PASSWORD, else prompt on a terminal, else CEC-only).
CEC_SERVICE_NAME="piframe-cec"
CEC_ENV_FILE="/etc/piframe/cec.env"
MQTT_HOST=""
MQTT_USER=""
MQTT_PASSWORD="${PIFRAME_MQTT_PASSWORD:-}"
MQTT_HOST_DEFAULT="192.168.130.11"
MQTT_USER_DEFAULT="piframe"

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
  --mqtt-host <host>     MQTT broker for TV power control (piframe-cec).
                         Default: existing ${CEC_ENV_FILE}, else ${MQTT_HOST_DEFAULT}
  --mqtt-user <name>     MQTT login. Default: existing env file, else ${MQTT_USER_DEFAULT}
  --mqtt-password <pw>   MQTT password. Prefer PIFRAME_MQTT_PASSWORD or the
                         interactive prompt (flags show up in ps). Re-runs
                         reuse the one already in ${CEC_ENV_FILE}.
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
    --mqtt-host)
      MQTT_HOST="$2"
      shift 2
      ;;
    --mqtt-user)
      MQTT_USER="$2"
      shift 2
      ;;
    --mqtt-password)
      MQTT_PASSWORD="$2"
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

# ----------------------------------------------------------------
# Resolve the MQTT settings for piframe-cec. Same inheritance idea as
# orientation: an explicit flag wins, else the value already in the
# env file (a routine re-run must not need the password again), else
# the default. A missing password prompts on a terminal; with no
# terminal the service still installs and runs CEC-only (it wakes the
# TV after power returns but Home Assistant can't reach it) until a
# re-run supplies one.
# ----------------------------------------------------------------
env_file_value() {
  [[ -f "${CEC_ENV_FILE}" ]] || return 0
  sed -n "s/^$1=//p" "${CEC_ENV_FILE}" | head -1
}
[[ -n "${MQTT_HOST}" ]] || MQTT_HOST="$(env_file_value PIFRAME_MQTT_HOST)"
[[ -n "${MQTT_USER}" ]] || MQTT_USER="$(env_file_value PIFRAME_MQTT_USER)"
[[ -n "${MQTT_PASSWORD}" ]] || MQTT_PASSWORD="$(env_file_value PIFRAME_MQTT_PASSWORD)"
MQTT_HOST="${MQTT_HOST:-${MQTT_HOST_DEFAULT}}"
MQTT_USER="${MQTT_USER:-${MQTT_USER_DEFAULT}}"
if [[ -z "${MQTT_PASSWORD}" && -t 0 ]]; then
  echo
  echo "TV power control logs in to the MQTT broker at ${MQTT_HOST} as '${MQTT_USER}'."
  read -r -s -p "MQTT password (Enter to skip; TV control then stays local-only): " MQTT_PASSWORD || MQTT_PASSWORD=""
  echo
fi
if [[ -z "${MQTT_PASSWORD}" ]]; then
  echo "WARNING: no MQTT password - ${CEC_SERVICE_NAME} will run CEC-only (no Home Assistant). Re-run with PIFRAME_MQTT_PASSWORD=... to connect it." >&2
fi

VNC_SERVICE_FILE="/etc/systemd/system/${VNC_SERVICE_NAME}.service"
VNC_CONFIG_DIR="${USER_HOME}/.config/wayvnc"
VNC_CONFIG_FILE="${VNC_CONFIG_DIR}/config"
ORIGIN_URL="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"

# ----------------------------------------------------------------
# Package-state preflight. An apt upgrade interrupted mid-run (on
# 2026-09-20 the Trixie upgrade took Wi-Fi down halfway through and
# the reboot killed dpkg) leaves packages unpacked-but-unconfigured.
# Two things then break silently: a kernel is half-installed, and the
# Raspberry Pi OS sudoers rule vanishes (raspberrypi-sys-mods owns
# /etc/sudoers.d/010_pi-nopasswd), so update.sh's `sudo systemctl
# restart` starts asking for a password and OTA looks like it worked
# while the old process keeps running. Finish that transaction before
# touching anything else. This is NOT an upgrade - it only completes
# what dpkg already started. Policy: the frames are not apt-upgraded
# as routine hygiene (see README "OS packages").
# ----------------------------------------------------------------
# The configure step runs as a transient systemd unit, not in this
# shell: configuring network-manager restarts NetworkManager, which
# drops Wi-Fi and kills an SSH session (and with it a script running
# inside it) right in the middle of dpkg - which is exactly how the
# frames got half-configured in the first place. As a unit, dpkg
# finishes even if this session dies; re-run bootstrap afterwards.
# Do NOT power-cycle the frame while the log is still growing.
# ----------------------------------------------------------------
# Hardware watchdog: 3 minutes, not Raspberry Pi OS's 1 minute
# (/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf). On
# network-manager 1.52.1-1+rpt4 every NetworkManager restart makes NM
# ask systemd for a daemon-reload that deadlocks in the generator
# sandbox for exactly 90s ("Failed to fork off sandboxing environment
# for executing generators: Protocol error", "Reloading finished in
# 90192 ms"). PID1 can't pet the watchdog meanwhile, so at 60s the
# board hard-resets - that is what kept stairs + both living-room
# frames half-configured from 2026-09-20 to 09-22: configuring
# network-manager restarts NM, the frame reset ~60s later, dpkg never
# finished, and the next attempt did the same. A boot-time NM start
# does not trigger it. 3 minutes clears the 90s stall and still
# catches a genuinely hung system. Written before the dpkg preflight
# and applied to the running manager too (no reboot needed), so the
# preflight below is protected on its first run.
# ----------------------------------------------------------------
WATCHDOG_CONF="/etc/systemd/system.conf.d/50-piframe-watchdog.conf"
install -d -m 0755 "$(dirname "${WATCHDOG_CONF}")"
cat > "${WATCHDOG_CONF}" <<'EOF'
# piframe - written by bootstrap_pi.sh; see the watchdog note there.
# Overrides 40-rpi-enable-watchdog.conf (later name wins).
[Manager]
RuntimeWatchdogSec=3min
EOF
busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 \
  org.freedesktop.systemd1.Manager RuntimeWatchdogUSec t 180000000 2>/dev/null \
  || echo "WARNING: could not apply the 3-minute watchdog to the running system; it takes effect at next boot" >&2
WATCHDOG_STATE="$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null || echo unknown)"

DPKG_PREFLIGHT_LOG="/var/log/piframe-dpkg-preflight.log"
if [[ -n "$(dpkg --audit 2>/dev/null)" ]]; then
  echo "dpkg reports unconfigured packages - finishing the interrupted install first."
  echo "  (runs as unit piframe-dpkg-preflight; log: ${DPKG_PREFLIGHT_LOG})"
  echo "  If this SSH session drops, WAIT - do not reboot - then reconnect and re-run bootstrap."
  systemctl reset-failed piframe-dpkg-preflight.service 2>/dev/null || true
  systemd-run --unit=piframe-dpkg-preflight --wait --collect --quiet     -p StandardOutput=file:"${DPKG_PREFLIGHT_LOG}" -p StandardError=file:"${DPKG_PREFLIGHT_LOG}"     -E DEBIAN_FRONTEND=noninteractive     /bin/bash -c 'dpkg --configure -a && apt-get -y -f install'     || { echo "dpkg preflight failed - see ${DPKG_PREFLIGHT_LOG}" >&2; exit 1; }
  echo "dpkg preflight complete."
fi

if [[ ${INSTALL_SYSTEM_PACKAGES} -eq 1 ]]; then
  apt-get update
  # --no-upgrade: a re-run on a configured frame must only fill in
  # missing packages, never bump ones already installed (see the
  # no-apt-upgrade policy in README "OS packages").
  apt-get install -y --no-upgrade \
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
# Ordering only (After=, no Wants=). wayvnc restarts every 2s with no
# start-limit cap, and a Wants= here re-pulled the kiosk unit into
# every one of those restarts - so a deliberately stopped/disabled
# piframe-client came straight back. The kiosk unit is enabled on its
# own; this unit just attaches to whatever cage session exists.
After=${SERVICE_NAME}.service
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
# piframe-cec: TV power over HDMI-CEC for Home Assistant. Its own unit
# on purpose - a kiosk restart (OTA, crash, mode swap) never blips the
# TV, and screen control keeps working when the kiosk is broken. Same
# checkout + venv as the client, so self-update ships it too (update.sh
# restarts it). No per-frame settings: it finds whichever HDMI port has
# the TV and learns its input from the TV. StateDirectory holds the
# last desired on/off so a power cut recovers before HA is back up.
# ----------------------------------------------------------------
CEC_SERVICE_FILE="/etc/systemd/system/${CEC_SERVICE_NAME}.service"
install -d -m 0755 "$(dirname "${CEC_ENV_FILE}")"
(
  umask 077
  cat > "${CEC_ENV_FILE}" <<EOF
# piframe-cec - written by bootstrap_pi.sh; re-run bootstrap to change.
PIFRAME_MQTT_HOST=${MQTT_HOST}
PIFRAME_MQTT_PORT=1883
PIFRAME_MQTT_USER=${MQTT_USER}
PIFRAME_MQTT_PASSWORD=${MQTT_PASSWORD}
EOF
)
chown root:root "${CEC_ENV_FILE}"
chmod 0600 "${CEC_ENV_FILE}"

cat > "${CEC_SERVICE_FILE}" <<EOF
[Unit]
Description=PiFrame CEC (TV power over HDMI-CEC for Home Assistant)
# Soft network dependency: CEC works without the broker, and paho
# reconnects on its own once the network is up.
After=network-online.target
Wants=network-online.target

[Service]
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/python ${REPO_DIR}/piframe_cec.py
EnvironmentFile=-${CEC_ENV_FILE}
Environment=PYTHONUNBUFFERED=1
StateDirectory=${CEC_SERVICE_NAME}
Restart=always
RestartSec=5

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

# ----------------------------------------------------------------
# Passwordless sudo for exactly what the client needs. update.sh and
# the Restart action run `sudo systemctl restart piframe-client`; the
# on-device maintenance chord runs `sudo systemctl stop piframe-vnc
# piframe-client`. Raspberry Pi OS's own 010_pi-nopasswd (NOPASSWD:
# ALL) used to cover this by accident, but that file belongs to
# raspberrypi-sys-mods and disappeared on three frames when its
# upgrade was interrupted. A file we own, scoped to the two units,
# survives whatever the OS packages do. Both /bin and /usr/bin
# spellings are listed: sudo matches the path as invoked, and
# update.sh calls /bin/systemctl while the client calls bare
# systemctl (resolved via PATH to /usr/bin).
# ----------------------------------------------------------------
SUDOERS_FILE="/etc/sudoers.d/020_piframe"
sudoers_cmds=""
for bin in /usr/bin/systemctl /bin/systemctl; do
  for spec in \
    "restart ${SERVICE_NAME}" \
    "restart ${VNC_SERVICE_NAME}" \
    "stop ${VNC_SERVICE_NAME} ${SERVICE_NAME}" \
    "start ${SERVICE_NAME} ${VNC_SERVICE_NAME}" \
    "stop ${SERVICE_NAME}" \
    "start ${SERVICE_NAME}" \
    "stop ${VNC_SERVICE_NAME}" \
    "start ${VNC_SERVICE_NAME}" \
    "restart ${CEC_SERVICE_NAME}" \
    "stop ${CEC_SERVICE_NAME}" \
    "start ${CEC_SERVICE_NAME}"; do
    sudoers_cmds="${sudoers_cmds:+${sudoers_cmds}, }${bin} ${spec}"
  done
done
cat > "${SUDOERS_FILE}.tmp" <<EOF
# piframe kiosk - written by bootstrap_pi.sh; re-run bootstrap to change.
${SERVICE_USER} ALL=(root) NOPASSWD: ${sudoers_cmds}
EOF
chmod 0440 "${SUDOERS_FILE}.tmp"
if visudo -c -q -f "${SUDOERS_FILE}.tmp"; then
  mv "${SUDOERS_FILE}.tmp" "${SUDOERS_FILE}"
  echo "sudoers: ${SUDOERS_FILE} (systemctl restart/stop/start for the kiosk + CEC units)"
else
  rm -f "${SUDOERS_FILE}.tmp"
  echo "WARNING: generated sudoers file failed visudo validation; sudo left unchanged" >&2
fi

# ----------------------------------------------------------------
# Wi-Fi: move an imager-written netplan profile to a native
# NetworkManager keyfile. Raspberry Pi Imager stores Wi-Fi as
# /etc/netplan/90-NM-<uuid>.yaml and NetworkManager regenerates its
# real profile from it on every boot. The 2026-09-20 Trixie upgrade
# (network-manager +rpt4 netplan sync, netplan.io +rpt1) regenerated
# that profile WITHOUT its key - every frame then failed with
# "no-secrets". A keyfile under /etc/NetworkManager/system-connections
# with the PSK stored is what `nmcli device wifi connect` writes and
# is not touched by netplan. The migration copies the live runtime
# profile (settings + psk), activates it, then deletes the netplan
# one; originals are backed up to /root/wifi-backup. It runs DETACHED
# (systemd-run) because switching connections briefly drops the link
# and would kill this script when bootstrap is run over Wi-Fi SSH.
# Idempotent: nothing to do when /etc/netplan has no wifi profile.
# ----------------------------------------------------------------
WIFI_MIGRATE_LOG="/var/log/piframe-wifi-migrate.log"
if ls /etc/netplan/90-NM-*.yaml >/dev/null 2>&1 && grep -lq "^  wifis:" /etc/netplan/90-NM-*.yaml 2>/dev/null; then
  cat > /usr/local/sbin/piframe-wifi-migrate <<'MIGRATE'
#!/usr/bin/env bash
# Written by bootstrap_pi.sh. Never prints the PSK.
set -euo pipefail
BACKUP=/root/wifi-backup
CONN_DIR=/etc/NetworkManager/system-connections
mkdir -p "$BACKUP"
for yaml in /etc/netplan/90-NM-*.yaml; do
  grep -q "^  wifis:" "$yaml" || continue
  uuid="$(sed -nE 's/^[[:space:]]*uuid: "?([0-9a-f-]{36})"?.*/\1/p' "$yaml" | head -1)"
  [[ -n "$uuid" ]] || { echo "skip $yaml: no uuid"; continue; }
  runtime="$(ls /run/NetworkManager/system-connections/netplan-NM-${uuid}-*.nmconnection 2>/dev/null | head -1 || true)"
  [[ -n "$runtime" ]] || { echo "skip $yaml: no runtime profile for $uuid (not loaded?)"; continue; }
  grep -q '^psk=' "$runtime" || { echo "skip $yaml: runtime profile carries no psk"; continue; }
  ssid="$(sed -nE 's/^ssid=(.*)$/\1/p' "$runtime" | head -1)"
  [[ -n "$ssid" ]] || { echo "skip $yaml: no ssid"; continue; }
  newid="$ssid"
  newfile="$CONN_DIR/${newid}.nmconnection"
  if [[ -e "$newfile" ]]; then
    echo "skip $yaml: $newfile already exists"; continue
  fi
  cp -a "$yaml" "$runtime" "$BACKUP/"
  newuuid="$(cat /proc/sys/kernel/random/uuid)"
  umask 077
  sed -E -e '/^#Netplan/d' -e "s/^uuid=.*/uuid=${newuuid}/" -e "s/^id=.*/id=${newid}/" "$runtime" > "$newfile"
  chown root:root "$newfile"; chmod 600 "$newfile"
  nmcli connection load "$newfile"
  echo "activating '$newid' (from netplan profile $uuid)"
  nmcli connection up "$newid"
  sleep 6
  nmcli connection delete uuid "$uuid" || rm -f "$yaml"
  echo "migrated $yaml -> $newfile"
done
echo "netplan dir now:"; ls -la /etc/netplan/
echo "active:"; nmcli -t -f NAME,DEVICE,STATE connection show --active
echo WIFI_MIGRATE_DONE
MIGRATE
  chmod 0755 /usr/local/sbin/piframe-wifi-migrate
  echo "Wi-Fi: netplan profile found - migrating to a NetworkManager keyfile in the background (log: ${WIFI_MIGRATE_LOG})"
  systemctl reset-failed piframe-wifi-migrate.service 2>/dev/null || true
  systemd-run --unit=piframe-wifi-migrate --collect \
    -p StandardOutput=file:"${WIFI_MIGRATE_LOG}" -p StandardError=file:"${WIFI_MIGRATE_LOG}" \
    /usr/local/sbin/piframe-wifi-migrate >/dev/null 2>&1 || \
    echo "WARNING: could not launch the Wi-Fi migration unit; run /usr/local/sbin/piframe-wifi-migrate as root by hand" >&2
fi

# ----------------------------------------------------------------
# NAS mount check. The kiosk loads every slide over file:// from
# ${NAS_ROOT}; if the share isn't mounted the page paints black while
# the client happily reports "slideshow" to the manager. The mount
# unit is nofail on purpose (a slow NAS must not block boot), so a
# failure is silent unless someone looks. Try once more now that the
# network is up, then WARN loudly with the fstab line, the unit's
# last words and a fix hint when the fstab path drifted from the fleet.
# Never fatal: the frame still boots to the idle page without it.
# ----------------------------------------------------------------
NAS_MOUNT_STATE="not configured"
if [[ -n "${MOUNT_UNIT}" ]]; then
  if ! systemctl is-active --quiet "${MOUNT_UNIT}"; then
    systemctl reset-failed "${MOUNT_UNIT}" 2>/dev/null || true
    systemctl start "${MOUNT_UNIT}" 2>/dev/null || true
  fi
  if mountpoint -q "${NAS_ROOT}"; then
    NAS_MOUNT_STATE="active (${NAS_ROOT})"
  else
    NAS_MOUNT_STATE="FAILED - see warning above"
    fstab_line="$(grep -E "[[:space:]]${NAS_ROOT}[[:space:]]" /etc/fstab 2>/dev/null || true)"
    echo "" >&2
    echo "WARNING: ${NAS_ROOT} is not mounted (${MOUNT_UNIT} failed)." >&2
    echo "  The kiosk will show a black screen for every playlist until this is fixed." >&2
    if [[ -n "${fstab_line}" ]]; then
      echo "  fstab: ${fstab_line}" >&2
      fstab_share="$(printf '%s' "${fstab_line}" | awk '{print $1}')"
      if [[ -n "${NAS_SHARE_EXPECTED}" && "${fstab_share}" != "${NAS_SHARE_EXPECTED}" ]]; then
        echo "  The fleet mounts ${NAS_SHARE_EXPECTED} - this frame's fstab points elsewhere." >&2
        echo "  Fix: sudo sed -i 's#${fstab_share}#${NAS_SHARE_EXPECTED}#' /etc/fstab && sudo systemctl daemon-reload && sudo systemctl start ${MOUNT_UNIT}" >&2
      fi
    else
      echo "  No fstab entry for ${NAS_ROOT}. Expected something like:" >&2
      echo "  ${NAS_SHARE_EXPECTED}  ${NAS_ROOT}  cifs  credentials=/root/.nascred,vers=3.0,iocharset=utf8,_netdev,nofail  0  0" >&2
    fi
    echo "  Unit log:" >&2
    journalctl -u "${MOUNT_UNIT}" -b --no-pager 2>/dev/null | tail -4 | sed 's/^/    /' >&2
    echo "" >&2
  fi
fi

systemctl daemon-reload
systemctl enable --now seatd
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl enable "${VNC_SERVICE_NAME}"
systemctl restart "${VNC_SERVICE_NAME}"
systemctl enable "${CEC_SERVICE_NAME}"
systemctl restart "${CEC_SERVICE_NAME}"

# ----------------------------------------------------------------
# CEC check, next to the NAS one: catch a TV with HDMI-CEC switched off
# at setup rather than the first time Home Assistant can't turn it off.
# Read-only (vendor + power queries); the screen doesn't change. Waits
# for the service to claim its CEC address first. Never fatal.
# ----------------------------------------------------------------
sleep 4
CEC_STATE="$(sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/python" "${REPO_DIR}/piframe_cec.py" --probe 2>&1 | tail -1 || true)"
case "${CEC_STATE}" in
  *"TV answering"*) ;;
  *)
    echo "" >&2
    echo "WARNING: ${CEC_STATE:-CEC probe produced no output}" >&2
    echo "  Home Assistant won't be able to switch this TV until the TV answers CEC." >&2
    echo "  Samsung: Settings > General > External Device Manager > Anynet+ (HDMI-CEC) = On," >&2
    echo "  and Settings > General > Power and Energy Saving > Power Button Option = On/Off" >&2
    echo "  (so standby turns it off instead of into Art Mode). Menu names vary by model year." >&2
    echo "  Then re-check: ${VENV_DIR}/bin/python ${REPO_DIR}/piframe_cec.py --probe" >&2
    echo "" >&2
    ;;
esac

cat <<EOF

PiFrame bootstrap complete.

Repo dir:        ${REPO_DIR}
Service user:    ${SERVICE_USER}
Service file:    ${SERVICE_FILE}
Server URL:      ${SERVER_URL}
NAS root:        ${NAS_ROOT}
NAS mount:       ${NAS_MOUNT_STATE}
Orientation:     transform=${OUTPUT_TRANSFORM_VALUE}
Audio device:    ${ALSA_DEVICE}
VNC service:     ${VNC_SERVICE_FILE} (listening on ${VNC_LISTEN_ADDRESS}:${VNC_LISTEN_PORT})
VNC config:      ${VNC_CONFIG_FILE}
CEC service:     ${CEC_SERVICE_FILE} (MQTT ${MQTT_USER}@${MQTT_HOST}$([[ -n "${MQTT_PASSWORD}" ]] || echo ', NO PASSWORD - CEC-only'))
CEC check:       ${CEC_STATE}
Sudoers:         ${SUDOERS_FILE}
Watchdog:        ${WATCHDOG_STATE} (${WATCHDOG_CONF})
Wi-Fi migration: ${WIFI_MIGRATE_LOG} (only written when a netplan Wi-Fi profile was found)

Useful checks:
  systemctl status ${SERVICE_NAME} --no-pager
  journalctl -u ${SERVICE_NAME} -f
  systemctl status ${VNC_SERVICE_NAME} --no-pager
  journalctl -u ${CEC_SERVICE_NAME} -f
  pactl get-sink-volume @DEFAULT_SINK@

If audio does not work on the target Pi, verify the ALSA device with: aplay -l
EOF
