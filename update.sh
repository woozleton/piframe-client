#!/bin/bash
# In-place updater for the piframe client.
#
# Triggered by the WebSocket `update_self` command from the server's
# System tab. Fetches origin/main from GitHub and resets the working
# tree to it, then asks systemd to restart the service.
#
# After a successful pull, writes `/tmp/piframe_last_update.json` with
# from/to SHAs, the list of changed files, and a one-line summary per
# new commit. The client reads that file on boot and ships it home in
# the next status_update heartbeat so the System tab can show what
# changed and whether the restart took.
#
# Pi setup (one-time, per device):
#   * Repo lives at a known path with a clean working tree.
#   * Deploy key (or HTTPS creds) is configured so `git pull` doesn't prompt.
#   * Sudoers entry allows the service user to restart without a password.
#   * Service unit has `Restart=always` so the in-flight restart self-heals
#     even if the new code crashes on boot.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="${PIFRAME_SERVICE_NAME:-piframe-client}"
LAST_UPDATE_FILE="${PIFRAME_LAST_UPDATE_FILE:-/tmp/piframe_last_update.json}"

cd "$REPO_DIR"

before_full="$(git rev-parse HEAD)"
before_short="$(git rev-parse --short HEAD)"
echo "[update] repo=$REPO_DIR service=$SERVICE_NAME"
echo "[update] before: $before_short"

# Fail fast on dirty working tree - we don't want to silently nuke local
# debugging changes. Operator can resolve manually via SSH.
if [ -n "$(git status --porcelain)" ]; then
  echo "[update] working tree is dirty - aborting"
  exit 2
fi

git fetch --quiet origin main
git reset --hard origin/main

after_full="$(git rev-parse HEAD)"
after_short="$(git rev-parse --short HEAD)"
echo "[update] after:  $after_short"

# Already up to date - no restart needed and nothing useful to record.
# Still drop a marker file so the UI can show "checked X, no change"
# rather than implying we never got the click.
if [ "$before_full" = "$after_full" ]; then
  echo "[update] already up to date, skipping restart"
  python3 - <<PY
import json, time
payload = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "from": "$before_short",
    "to": "$after_short",
    "files": [],
    "commits": [],
    "noop": True,
}
with open("$LAST_UPDATE_FILE", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY
  exit 0
fi

# Collect changed files + commit subjects across the range. Using NUL
# separators so paths or commit subjects with embedded newlines/spaces
# don't tear up the JSON.
export FILES_NUL="$(git diff --name-only -z "$before_full" "$after_full" || true)"
export COMMITS_NUL="$(git log --reverse --format=%h%x09%s%x00 "$before_full..$after_full" || true)"

# Hand off to python3 for JSON encoding - bash heredoc + json.dump is
# more robust than hand-rolled escaping, and python3 ships on every Pi
# that runs this client anyway.
python3 - <<PY
import json, os, time

before_short = "$before_short"
after_short = "$after_short"

raw_files = os.environ.get("FILES_NUL", "")
files = [p for p in raw_files.split("\x00") if p]

raw_commits = os.environ.get("COMMITS_NUL", "")
commits = []
for chunk in raw_commits.split("\x00"):
    chunk = chunk.strip()
    if not chunk:
        continue
    if "\t" in chunk:
        sha, subject = chunk.split("\t", 1)
    else:
        sha, subject = chunk, ""
    commits.append({"sha": sha, "subject": subject})

payload = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "from": before_short,
    "to": after_short,
    "files": files,
    "commits": commits,
    "noop": False,
}
with open("$LAST_UPDATE_FILE", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY

# `exec` so the script's PID is replaced; systemctl will fire the
# restart and our parent process (the running client) gets killed.
exec sudo /bin/systemctl restart "$SERVICE_NAME"
