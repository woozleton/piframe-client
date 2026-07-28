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
#   * Repo lives at a known path (~/piframe_client). Local drift is
#     archived to the logfile and clobbered on update - this checkout
#     is a deployment artifact, not a workspace.
#   * Deploy key (or HTTPS creds) is configured so `git pull` doesn't prompt.
#   * Sudoers entry allows the service user to restart without a password.
#   * Service unit has `Restart=always` so the in-flight restart self-heals
#     even if the new code crashes on boot.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="${PIFRAME_SERVICE_NAME:-piframe-client}"
LAST_UPDATE_FILE="${PIFRAME_LAST_UPDATE_FILE:-/tmp/piframe_last_update.json}"
LOG_FILE="${PIFRAME_UPDATE_LOG:-/tmp/piframe_update.log}"

# Mirror everything from here on into a known logfile. The client spawns
# this script with stdout/stderr -> /dev/null so a silent failure leaves
# nothing in journalctl; this gives us something to read after the fact.
exec > >(tee -a "$LOG_FILE") 2>&1
echo "==== $(date -u '+%Y-%m-%dT%H:%M:%SZ') update.sh start ===="

cd "$REPO_DIR"

# Pin LF for tracked text on this checkout so a Windows-side commit
# (where core.autocrlf=true rewrites *.sh to CRLF) doesn't leave the
# Pi's working tree permanently "modified" after checkout. Idempotent.
git config --local core.autocrlf input

before_full="$(git rev-parse HEAD)"
before_short="$(git rev-parse --short HEAD)"
echo "[update] repo=$REPO_DIR service=$SERVICE_NAME"
echo "[update] before: $before_short"

# Refresh the index so files that look "modified" only because of stat
# drift (mtime, mode, EOL settings just changed) reset to clean. After
# this, `git status --porcelain` reflects genuine content drift only.
git update-index --refresh >/dev/null 2>&1 || true

dirty="$(git status --porcelain)"
if [ -n "$dirty" ]; then
  # Real content drift remains after refresh. This checkout is a
  # deployment artifact, not a workspace - the operator clicked Update,
  # which means "run origin/main". Archive the drift into the logfile
  # (status + full diff), then clobber. `git clean -fd` clears untracked
  # non-ignored files too, so a stray file can't collide with a future
  # tracked path; WITHOUT -x, so ignored state (api-env/, logs,
  # client_settings.json) survives.
  echo "[update] working tree is dirty - archiving drift to this log, then clobbering"
  echo "$dirty"
  echo "[update] ---- drift diff start ----"
  git diff || true
  echo "[update] ---- drift diff end ----"
  git clean -fd || true
fi

# Bound the fetch so a dead network path (DNS, IPv6 black-hole, GitHub
# outage) fails loudly here instead of hanging forever and surfacing as
# a generic server-side timeout with an empty log.
if ! timeout 45 git fetch --quiet origin main; then
  echo "[update] git fetch failed or timed out (network path to origin?) - aborting"
  exit 3
fi
git reset --hard origin/main

after_full="$(git rev-parse HEAD)"
after_short="$(git rev-parse --short HEAD)"
echo "[update] after:  $after_short"

# Already up to date - no restart needed. Drop a marker so the UI can
# show "already up to date" instead of implying we never got the click.
if [ "$before_full" = "$after_full" ]; then
  echo "[update] already up to date, skipping restart"
  BEFORE_SHORT="$before_short" AFTER_SHORT="$after_short" LAST_UPDATE_FILE="$LAST_UPDATE_FILE" python3 - <<'PY'
import json, os, time
payload = {
    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "from": os.environ["BEFORE_SHORT"],
    "to": os.environ["AFTER_SHORT"],
    "files": [],
    "commits": [],
    "noop": True,
}
with open(os.environ["LAST_UPDATE_FILE"], "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY
  exit 0
fi

# Collect changed files + commit subjects across the range. NUL
# separators so paths or subjects with embedded whitespace don't tear up
# the JSON encoding step downstream.
export FILES_NUL="$(git diff --name-only -z "$before_full" "$after_full" || true)"
export COMMITS_NUL="$(git log --reverse --format=%h%x09%s%x00 "$before_full..$after_full" || true)"
export BEFORE_SHORT="$before_short"
export AFTER_SHORT="$after_short"
export LAST_UPDATE_FILE

# Hand off to python3 for JSON encoding - safer than hand-rolled
# escaping, and python3 ships on every Pi that runs this client.
# Heredoc is single-quoted so $vars inside are read from python's
# os.environ, not bash-interpolated at heredoc expansion time.
python3 - <<'PY'
import json, os, time

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
    "from": os.environ["BEFORE_SHORT"],
    "to": os.environ["AFTER_SHORT"],
    "files": files,
    "commits": commits,
    "noop": False,
}
with open(os.environ["LAST_UPDATE_FILE"], "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY

echo "[update] handing off to systemctl restart"
# `exec` so the script's PID is replaced; systemctl will fire the
# restart and our parent process (the running client) gets killed.
exec sudo /bin/systemctl restart "$SERVICE_NAME"
