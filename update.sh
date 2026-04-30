#!/bin/bash
# In-place updater for the piframe client.
#
# Triggered by the WebSocket `update_self` command from the server's
# System tab. Fetches origin/main from GitHub and resets the working
# tree to it, then asks systemd to restart the service.
#
# Pi setup (one-time, per device):
#   * Repo lives at a known path with a clean working tree.
#   * Deploy key (or HTTPS creds) is configured so `git pull` doesn't prompt.
#   * Sudoers entry allows the service user to restart without a password:
#       woozleton ALL=NOPASSWD: /bin/systemctl restart piframe-client
#   * Service unit has `Restart=always` so the in-flight restart self-heals
#     even if the new code crashes on boot.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="${PIFRAME_SERVICE_NAME:-piframe-client}"

cd "$REPO_DIR"

echo "[update] repo=$REPO_DIR service=$SERVICE_NAME"
echo "[update] before: $(git rev-parse --short HEAD)"

# Fail fast on dirty working tree - we don't want to silently nuke local
# debugging changes. Operator can resolve manually via SSH.
if [ -n "$(git status --porcelain)" ]; then
  echo "[update] working tree is dirty - aborting"
  exit 2
fi

git fetch --quiet origin main
git reset --hard origin/main

echo "[update] after:  $(git rev-parse --short HEAD)"

# `exec` so the script's PID is replaced; systemctl will fire the
# restart and our parent process (the running client) gets killed.
exec sudo /bin/systemctl restart "$SERVICE_NAME"
