#!/usr/bin/env bash
# The read-only copy of the tracker on the private machine that runs the executive-assistant agent. The real
# tracker stays on this Mac. Run from the Mac.
#   deploy/tailnet/server.sh deploy   ship HEAD to the server and restart the copy (do this after changing the tracker's
#                                     code, so the copy reads the database with the same code that wrote it)
#   deploy/tailnet/server.sh status   is it running, which commit, and when its data was last synced from this Mac
#   deploy/tailnet/server.sh logs
#   deploy/tailnet/server.sh remove   stop it and delete the copied data from the server
# The data is brought over by the agent project (executive-assistant-agent/deploy/server/sync-tracker-copy.sh,
# every five minutes on the server, through the Mac bridge); that script also starts the copy the first time.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="${TRACKER_SERVER:-atlas-server}"
ROOT=/opt/tracker
SHARED="${TRACKER_SHARED:-$HOME/Documents/agent-coding/shared}"
INBOX='/opt/atlas/data/home/Library/Application Support/LLMInbox'
COMPOSE="docker compose -f $ROOT/app/deploy/tailnet/docker-compose.yml --env-file $ROOT/env"
r() { ssh "$SERVER" "$@"; }
die() { echo "tracker copy: $*" >&2; exit 1; }
mac_tz() { readlink /etc/localtime | sed 's|.*/zoneinfo/||'; }

case "${1:-}" in
deploy)
  [ -f "$SHARED/llm_contact.py" ] || die "agent-coding/shared not found at $SHARED"
  git -C "$REPO" diff --quiet HEAD -- tracker deploy requirements-server.txt || echo "note: uncommitted changes are NOT deployed; only HEAD is"
  stage="$(mktemp -d)"
  git -C "$REPO" archive HEAD tracker deploy/tailnet requirements-server.txt '*.example.md' | tar -x -C "$stage"
  mkdir -p "$stage/shared"
  cp "$SHARED/llm_contact.py" "$SHARED/llm-system-prompt-preamble.md" "$SHARED/contact_eli_tool.json" "$SHARED/note_to_eli_tool.json" "$stage/shared/"
  git -C "$REPO" log -1 --format='%h %s' > "$stage/VERSION"
  r "mkdir -p $ROOT/app $ROOT/data $ROOT/logs '$INBOX' && chown 1000:1000 $ROOT/data $ROOT/logs && chmod 700 $ROOT/data && { [ -f $ROOT/env ] || echo TRACKER_TZ=$(mac_tz) > $ROOT/env; }"
  rsync -az --delete "$stage/" "$SERVER:$ROOT/app/"; rm -rf "$stage"
  r "chmod -R a+rX $ROOT/app"            # mktemp's folder is private and rsync copies that; user 1000 must read the code
  r "$COMPOSE build -q"
  if r "test -f $ROOT/data/tracker.sqlite3"; then r "$COMPOSE up -d --force-recreate" >/dev/null 2>&1; echo "copy restarted on the server: $(r cat $ROOT/app/VERSION)"
  else echo "shipped $(r cat $ROOT/app/VERSION). It starts when the first sync from this Mac arrives (within five minutes)."; fi
  ;;
status)
  r "docker ps --format '{{.Names}} {{.Status}}' -f name=^tracker$; cat $ROOT/app/VERSION; cat $ROOT/env; curl -s -m 5 http://127.0.0.1:8791/api/replica | python3 -c 'import json,sys,time; d=json.load(sys.stdin); print(\"read-only copy:\", d[\"replica\"], \"| last synced\", round((time.time()-d[\"synced_at\"])/60), \"minutes ago\" if d[\"synced_at\"] else \"never\")'"
  ;;
logs) r "docker logs --tail 100 -f tracker" ;;
remove) r "$COMPOSE down; rm -rf $ROOT/data; rm -f /etc/cron.d/sync-tracker-copy"; echo "the copy and its data are gone from the server" ;;
*) sed -n '2,11p' "$0"; exit 1 ;;
esac
