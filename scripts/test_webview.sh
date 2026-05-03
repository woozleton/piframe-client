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
  echo "[test_webview] restoring piframe-client..."
  systemctl start piframe-client || true
}
trap cleanup EXIT

echo "[test_webview] stopping piframe-client (manager will see offline)..."
systemctl stop piframe-client

echo "[test_webview] opening webview${URL:+ at $URL}..."
echo "[test_webview] press Ctrl-C to close webview and resume normal service"
echo

# Run the BrowserController as the service user so XDG_RUNTIME_DIR /
# Wayland socket / PipeWire all match the normal piframe-client env.
sudo -u "${SERVICE_USER}" \
  XDG_RUNTIME_DIR="/run/user/${SERVICE_UID}" \
  "${VENV_PYTHON}" -c "
import signal, sys, os
sys.path.insert(0, '${REPO_DIR}')
from piframe_client import BrowserController
bc = BrowserController()
url = ${URL:+\"${URL}\"} or None
ok = bc.set_browser_mode('webview', url)
if not ok:
    print('[test_webview] set_browser_mode returned False', file=sys.stderr)
    sys.exit(1)
print(f'[test_webview] webview running (pid={os.getpid()}). Ctrl-C to exit.')
signal.signal(signal.SIGINT, lambda *a: (bc.shutdown(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *a: (bc.shutdown(), sys.exit(0)))
signal.pause()
"
