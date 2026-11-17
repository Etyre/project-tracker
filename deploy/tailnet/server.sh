#!/usr/bin/env bash
# The tracker's server on the private machine that runs the executive-assistant agent. Run from the Mac.
#   deploy/tailnet/server.sh deploy        ship HEAD and restart the server there
#   deploy/tailnet/server.sh move [--yes]  one time: quit the app here, copy the database, config and secrets to the
#                                          server, start it there, and restart the Mac app as a relay to it
#   deploy/tailnet/server.sh home [--yes]  the reverse
#   deploy/tailnet/server.sh backup        a database backup on the server now (daily at 05:15 otherwise)
#   deploy/tailnet/server.sh fetch         copy the server's newest backup into the Mac's data folder (backups/)
#   deploy/tailnet/server.sh status | logs
# Only one copy may hold the database: the Mac app with TASK_TRACKER_SERVER set never opens the local one.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="${TRACKER_SERVER:-atlas-server}"
ROOT=/opt/tracker
SHARED="${TRACKER_SHARED:-$HOME/Documents/agent-coding/shared}"
DATA="${TASK_TRACKER_DATA:-$HOME/Library/Application Support/TaskTracker}"
APP="$REPO/dist/Project Tracker.app"
LOCAL=http://127.0.0.1:8791
INBOX='/opt/atlas/data/home/Library/Application Support/LLMInbox'
COMPOSE="docker compose -f $ROOT/app/deploy/tailnet/docker-compose.yml --env-file $ROOT/env"
r() { ssh "$SERVER" "$@"; }
say() { printf '\n== %s\n' "$*"; }
die() { echo "tracker server: $*" >&2; exit 1; }
srv_running() { [ -n "$(r "docker ps -q -f name=^tracker$ -f status=running" 2>/dev/null)" ]; }
srv_url() { r "tailscale status --json" | python3 -c 'import json,sys; print("http://" + json.load(sys.stdin)["Self"]["DNSName"].rstrip(".") + ":8791")'; }
mac_tz() { readlink /etc/localtime | sed 's|.*/zoneinfo/||'; }
local_up() { curl -s --noproxy '*' -m 3 -o /dev/null "$LOCAL/api/status"; }
relay_mode() { grep -q '^TASK_TRACKER_SERVER=' "$DATA/env" 2>/dev/null; }

quit_app() {  # the launcher first (the shortest command line), or it restarts the Python it runs
  local pids; pids="$(ps -axo pid=,command= | awk -v p="$APP/Contents/MacOS/" 'index($0, p) {print length($0), $1}' | sort -n | awk '{print $2}')"
  [ -z "$pids" ] || kill $pids 2>/dev/null || true
  for _ in $(seq 1 20); do ps -axo command= | grep -qF "$APP/Contents/MacOS/" || return 0; sleep 1; done
  die "the Project Tracker app did not quit"
}
start_app() { open -g "$APP"; for _ in $(seq 1 30); do sleep 1; curl -s --noproxy '*' -m 2 -o /dev/null "$LOCAL/" && return 0; done; return 1; }
set_env() {  # set_env NAME [value]: set or (no value) remove a line in the Mac's data-folder env file
  touch "$DATA/env"; chmod 600 "$DATA/env"
  grep -v "^$1=" "$DATA/env" > "$DATA/env.tmp" || true
  [ -z "${2:-}" ] || echo "$1=$2" >> "$DATA/env.tmp"
  mv "$DATA/env.tmp" "$DATA/env"; chmod 600 "$DATA/env"
}

deploy() {
  [ -f "$SHARED/llm_contact.py" ] || die "agent-coding/shared not found at $SHARED"
  git -C "$REPO" diff --quiet HEAD -- tracker deploy requirements-server.txt || echo "note: uncommitted changes are NOT deployed; only HEAD is"
  stage="$(mktemp -d)"
  git -C "$REPO" archive HEAD tracker deploy/tailnet requirements-server.txt '*.example.md' | tar -x -C "$stage"
  mkdir -p "$stage/shared"
  cp "$SHARED/llm_contact.py" "$SHARED/llm-system-prompt-preamble.md" "$SHARED/contact_eli_tool.json" "$stage/shared/"
  git -C "$REPO" log -1 --format='%h %s' > "$stage/VERSION"
  r "mkdir -p $ROOT/app $ROOT/data $ROOT/logs '$INBOX' && chown 1000:1000 $ROOT/data $ROOT/logs && chmod 700 $ROOT/data && { [ -f $ROOT/env ] || echo TRACKER_TZ=$(mac_tz) > $ROOT/env; }"
  rsync -az --delete "$stage/" "$SERVER:$ROOT/app/"; rm -rf "$stage"
  r "chmod -R a+rX $ROOT/app"            # mktemp's folder is private and rsync copies that; user 1000 must read the code
  r "$COMPOSE build -q"
  if srv_running; then r "$COMPOSE up -d --force-recreate" >/dev/null 2>&1 && echo "restarted on the server: $(r cat $ROOT/app/VERSION)"
  else echo "shipped $(r cat $ROOT/app/VERSION). Not started: the tracker has not been moved to the server ($0 move)."; fi
}

case "${1:-}" in
deploy) deploy ;;
move)
  if [ "${2:-}" != "--yes" ]; then
    echo "This will, in order:"
    echo "  1. quit the Project Tracker app on this Mac"
    echo "  2. copy its database, config.toml, your two documents, the snapshots and its API keys (from the Keychain) to $SERVER"
    echo "  3. start the tracker's server there; if it does not answer, start the app here again as it was"
    echo "  4. restart the Mac app as a window onto the server (it then needs the network, and Tailscale)"
    echo "Run again with --yes. '$0 home --yes' undoes it."; exit 0
  fi
  relay_mode && die "this Mac's app already points at a server (TASK_TRACKER_SERVER in $DATA/env)"
  srv_running && die "the tracker is already running on the server"
  [ -d "$APP" ] || die "no app at $APP"
  deploy
  say "quitting the app on this Mac"
  quit_app
  say "copying the database, config, documents and keys"
  tmp="$(mktemp -d)"
  /usr/bin/sqlite3 "$DATA/tracker.sqlite3" ".backup '$tmp/tracker.sqlite3'"
  cp "$REPO/config.toml" "$tmp/config.toml"
  for f in "Rules and heuristics for scheduling.md" "Exercise plan.md"; do [ -f "$DATA/$f" ] && cp "$DATA/$f" "$tmp/"; done
  [ -d "$DATA/snapshots" ] && cp -R "$DATA/snapshots" "$tmp/snapshots"
  # API keys: the Mac keeps them in the Keychain (or the env file); the server has no Keychain, so its env file. Never printed.
  ( umask 077; : > "$tmp/env"
    [ -f "$DATA/env" ] && grep -v -E '^(TASK_TRACKER_SERVER=|#|$)' "$DATA/env" >> "$tmp/env" || true
    for pair in anthropic:ANTHROPIC_API_KEY toggl:TOGGL_API_TOKEN linear:LINEAR_API_KEY sheets:SHEETS_TOKEN agent:AGENT_API_TOKEN; do
      v="$(security find-generic-password -s TaskTracker -a "${pair%%:*}" -w 2>/dev/null || true)"
      [ -z "$v" ] || grep -q "^${pair##*:}=" "$tmp/env" || echo "${pair##*:}=$v" >> "$tmp/env"
    done )
  echo "keys copied: $(cut -d= -f1 "$tmp/env" | tr '\n' ' ')"
  rsync -az "$tmp/" "$SERVER:$ROOT/data/"; rm -rf "$tmp"
  r "chown -R 1000:1000 $ROOT/data && chmod 700 $ROOT/data && chmod 600 $ROOT/data/env"
  say "starting the tracker on the server"
  r "$COMPOSE up -d" 2>&1 | tail -1
  r "tailscale serve --bg --tcp=8791 tcp://localhost:8791 >/dev/null"
  r "echo '15 5 * * * root docker exec tracker python deploy/tailnet/backup.py >> /var/log/tracker-backup.log 2>&1' > /etc/cron.d/tracker-backup"
  ok=; for _ in $(seq 1 30); do r "curl -fsS -o /dev/null -m 4 http://127.0.0.1:8791/api/status" 2>/dev/null && ok=1 && break; sleep 2; done
  if [ -z "$ok" ]; then
    echo "!! the tracker did not answer on the server; starting the app on this Mac again, unchanged"
    r "docker logs --tail 30 tracker" || true; r "$COMPOSE down" || true
    start_app || true; exit 1
  fi
  say "restarting the Mac app as a window onto the server"
  set_env TASK_TRACKER_SERVER "$(srv_url)"
  start_app || echo "!! the Mac app did not come up; see ~/Library/Logs/project-tracker.log"
  echo "The tracker now runs on the server: $(srv_url) (from any of your Tailscale devices); the Mac app relays to it."
  ;;
home)
  if [ "${2:-}" != "--yes" ]; then echo "This will stop the tracker on $SERVER, copy its database, config and documents back, and run the app on this Mac as before. Run again with --yes."; exit 0; fi
  srv_running || die "the tracker is not running on the server"
  r "docker exec tracker python deploy/tailnet/backup.py" >/dev/null
  r "$COMPOSE down" 2>&1 | tail -1
  quit_app
  newest="$(r "ls -t $ROOT/data/backups/tracker-*.sqlite3 | head -1")"
  mkdir -p "$DATA/backups"; cp "$DATA/tracker.sqlite3" "$DATA/backups/before-coming-home-$(date +%Y%m%d-%H%M%S).sqlite3"
  rm -f "$DATA/tracker.sqlite3-wal" "$DATA/tracker.sqlite3-shm"
  rsync -az "$SERVER:$newest" "$DATA/tracker.sqlite3"
  cp "$REPO/config.toml" "$REPO/config.toml.before-coming-home"; rsync -az "$SERVER:$ROOT/data/config.toml" "$REPO/config.toml"
  rsync -az "$SERVER:$ROOT/data/"'*.md' "$DATA/" 2>/dev/null || true
  rsync -az "$SERVER:$ROOT/data/snapshots/" "$DATA/snapshots/" 2>/dev/null || true
  set_env TASK_TRACKER_SERVER
  start_app && echo "The tracker is back on this Mac. Keys changed on the server since the move are in $ROOT/data/env there, not in the Keychain."
  ;;
backup) r "docker exec tracker python deploy/tailnet/backup.py" ;;
fetch)
  newest="$(r "ls -t $ROOT/data/backups/tracker-*.sqlite3 | head -1")"; mkdir -p "$DATA/backups"
  rsync -az "$SERVER:$newest" "$DATA/backups/server-$(basename "$newest")" && echo "fetched $(basename "$newest") into $DATA/backups/"
  ;;
status)
  if srv_running; then echo "on the server: $(r cat $ROOT/app/VERSION); $(r cat $ROOT/env); Mac app relays: $(relay_mode && echo yes || echo NO)"
  else echo "not on the server; Mac app: $(local_up && echo running || echo not running)"; fi ;;
logs) r "docker logs --tail 100 -f tracker" ;;
*) sed -n '2,11p' "$0"; exit 1 ;;
esac
