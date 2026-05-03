#!/usr/bin/env bash
# Manual test driver for webview mode (run on the Pi via SSH).
#
# Stops piframe-client, drives BrowserController.set_browser_mode()
# directly, and on Ctrl-C restarts piframe-client so the device
# returns to normal manager-driven operation. The manager will see
# the device as offline while this script is running.
#
# Usage:
#   sudo ./scripts/test_webview.sh                          # opens about:blank
#   sudo ./scripts/test_webview.sh https://youtube.com      # opens that URL

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running with sudo..." >&2
  exec sudo "$0" "$@"
fi

URL="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PYTHON="${REPO_DIR}/api-env/bin/python"
SERVICE_USER="${SUDO_USER:-woozleton}"
SERVICE_UID="$(id -u "${SERVICE_USER}")"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Missing venv python at ${VENV_PYTHON}" >&2
  exit 1
fi

cleanup() {
  echo
  echo "[test_webview] tearing down test webview..."
  # Make sure no orphaned cage / chromium owns the Wayland socket
  # before systemd starts piframe-client - otherwise piframe-client's
  # cage gets "Unable to open Wayland socket" and the run loop sits
  # silent for a long time before retrying.
  pkill -TERM -f "/usr/bin/cage" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! pgrep -f "/usr/bin/cage" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  pkill -KILL -f "/usr/bin/cage" 2>/dev/null || true
  rm -f "/run/user/${SERVICE_UID}/wayland-"*
  echo "[test_webview] restoring piframe-client..."
  systemctl start piframe-client || true
  echo "[test_webview] waiting for kiosk Chromium to come back..."
  for _ in $(seq 1 30); do
    if pgrep -af "chromium.*--kiosk" >/dev/null 2>&1; then
      echo "[test_webview] kiosk back up."
      return
    fi
    sleep 1
  done
  echo "[test_webview] kiosk did not return within 30s. Check:"
  echo "  systemctl status piframe-client --no-pager"
  echo "  journalctl -u piframe-client -n 30 --no-pager"
}
trap cleanup EXIT

echo "[test_webview] stopping piframe-client (manager will see offline)..."
systemctl stop piframe-client

echo "[test_webview] opening webview${URL:+ at $URL}..."
echo "[test_webview] press Ctrl-C to close webview and resume normal service"
echo

# Run the BrowserController as the service user so XDG_RUNTIME_DIR /
# Wayland socket / PipeWire all match the normal piframe-client env.
# URL passes through as an env var so empty / quotes / special chars
# can't break the embedded Python.
sudo -u "${SERVICE_USER}" \
  XDG_RUNTIME_DIR="/run/user/${SERVICE_UID}" \
  PIFRAME_TEST_URL="${URL}" \
  PIFRAME_REPO_DIR="${REPO_DIR}" \
  "${VENV_PYTHON}" -c '
import os, signal, sys
sys.path.insert(0, os.environ["PIFRAME_REPO_DIR"])
from piframe_client import BrowserController
url = os.environ.get("PIFRAME_TEST_URL") or None
bc = BrowserController()
ok = bc.set_browser_mode("webview", url)
if not ok:
    print("[test_webview] set_browser_mode returned False", file=sys.stderr)
    sys.exit(1)
print(f"[test_webview] webview running (pid={os.getpid()}). Ctrl-C to exit.")
signal.signal(signal.SIGINT, lambda *a: (bc.shutdown(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *a: (bc.shutdown(), sys.exit(0)))
signal.pause()
'
