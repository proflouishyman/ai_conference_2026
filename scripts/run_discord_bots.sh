#!/bin/zsh
# Runs both Discord bots from cron. Built to survive a week unattended.
#
# Cron gets a minimal environment, so secrets are loaded explicitly. They come
# from ~/.conference_bots.env, which is deliberately independent of
# ~/.claude/settings.json: that file belongs to Claude Code and can be
# rewritten or moved without warning. A fallback to settings.json is kept so a
# missing env file degrades rather than breaks.
#
# Also: a lock so a slow run cannot overlap the next tick, log rotation so a
# week of output cannot fill the disk, and a heartbeat recording every run
# whether it succeeds or fails, so silence is distinguishable from success.

ROOT="/Users/louishyman/coding/ai_conference_2026"
LOGS="$ROOT/scripts/logs"
LOCK="/tmp/conference_discord_bots.lock"
HEARTBEAT="$LOGS/bots_heartbeat.log"
MAXLOG=1048576          # rotate at 1 MB

cd "$ROOT" || exit 1
mkdir -p "$LOGS"

# --- lock: skip this tick if the previous run is still going ----------------
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || exit 0   # stale, reclaim
  else
    echo "$(date '+%F %T') SKIP (previous run still active)" >> "$HEARTBEAT"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK"' EXIT INT TERM

# --- secrets ----------------------------------------------------------------
if [ -f "$HOME/.conference_bots.env" ]; then
  set -a; . "$HOME/.conference_bots.env"; set +a
fi
if [ -z "$OPENAI_API_KEY" ]; then
  K=$(/usr/bin/python3 -c "import json;print(json.load(open('$HOME/.claude/settings.json')).get('env',{}).get('OPENAI_API_KEY',''))" 2>/dev/null)
  [ -n "$K" ] && export OPENAI_API_KEY="$K"
fi

# --- rotate logs ------------------------------------------------------------
for f in "$LOGS/jargon_bot.log" "$LOGS/channel_bot.log" "$HEARTBEAT"; do
  if [ -f "$f" ] && [ "$(/usr/bin/stat -f%z "$f" 2>/dev/null || echo 0)" -gt "$MAXLOG" ]; then
    mv -f "$f" "$f.1"
  fi
done

# --- run --------------------------------------------------------------------
/usr/bin/python3 scripts/discord_jargon_bot.py  --apply >> "$LOGS/jargon_bot.log"  2>&1
J=$?
/usr/bin/python3 scripts/discord_channel_bot.py --apply >> "$LOGS/channel_bot.log" 2>&1
C=$?

KEYSTATE="key:ok"; [ -z "$OPENAI_API_KEY" ] && KEYSTATE="key:MISSING"
echo "$(date '+%F %T') jargon=$J channel=$C $KEYSTATE" >> "$HEARTBEAT"

[ $J -ne 0 ] || [ $C -ne 0 ] && exit 1
exit 0
