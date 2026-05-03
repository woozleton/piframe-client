#!/usr/bin/env bash
# Manual test driver for webview mode (run on the Pi via SSH).
#
# Sends webview_open / webview_close commands to the running
# piframe-client over its loopback control endpoint - the same
# in-process fast path the manager (Woozlescape) uses. No service
# restart, no cage cold-start. Mode swap takes ~2-3s, same as the
# manager-driven path.
#
# Usage:
#   sudo ./scripts/test_webview.sh                          # opens about:blank, then close on Ctrl-C
#   sudo ./scripts/test_webview.sh https://www.youtube.com  # opens that URL, then close on Ctrl-C
#   sudo ./scripts/test_webview.sh --close                  # close any active webview and exit
#
# Requires the piframe-client service to be running. If it's not
# running (e.g. the manager is unreachable on first boot), start it
# with `sudo systemctl start piframe-client` first - the WebSocket
# layer reconnects on its own retry loop and the kiosk comes up
# regardless of manager reachability.

set -euo pipefail

CONTROL_URL="http://127.0.0.1:18888/control"

post() {
  local payload="$1"
  curl --silent --show-error --fail \
    --max-time 10 \
    -H "Content-Type: application/json" \
    -d "${payload}" \
    "${CONTROL_URL}" >/dev/null
}

if [[ "${1:-}" == "--close" ]]; then
  post '{"cmd":"webview_close"}'
  echo "[test_webview] sent webview_close"
  exit 0
fi

URL="${1:-}"

if [[ -n "${URL}" ]]; then
  PAYLOAD="$(printf '{"cmd":"webview_open","params":{"url":"%s"}}' "${URL}")"
  echo "[test_webview] opening webview at ${URL}..."
else
  PAYLOAD='{"cmd":"webview_open"}'
  echo "[test_webview] opening webview (about:blank)..."
fi
post "${PAYLOAD}"
echo "[test_webview] press Ctrl-C to send webview_close"

cleanup() {
  echo
  echo "[test_webview] sending webview_close..."
  post '{"cmd":"webview_close"}' || true
  echo "[test_webview] done."
}
trap cleanup EXIT

# Just sleep; the trap fires on Ctrl-C and sends webview_close.
while true; do sleep 60; done
