#!/bin/zsh
# One-line health check for the Discord bots. Run this to see if they are alive.
LOGS="/Users/louishyman/coding/ai_conference_2026/scripts/logs"
HB="$LOGS/bots_heartbeat.log"

if [ ! -f "$HB" ]; then echo "NO HEARTBEAT FILE -- bots have never run"; exit 1; fi

LAST=$(tail -1 "$HB")
LASTEPOCH=$(/bin/date -j -f "%Y-%m-%d %H:%M:%S" "$(echo "$LAST" | cut -d' ' -f1-2)" "+%s" 2>/dev/null)
NOW=$(/bin/date "+%s")
AGE=$(( (NOW - LASTEPOCH) / 60 ))

echo "last run:  $LAST"
echo "age:       ${AGE} min ago"
if [ "$AGE" -gt 15 ]; then
  echo "STATUS:    STALE -- expected every 5 min. Check: crontab -l"
elif echo "$LAST" | grep -q "key:MISSING"; then
  echo "STATUS:    RUNNING but OPENAI KEY MISSING -- glossary answers only, no generation"
elif echo "$LAST" | grep -qv "jargon=0 channel=0"; then
  echo "STATUS:    RUNNING but a bot exited non-zero -- see logs below"
else
  echo "STATUS:    OK"
fi
echo
echo "pending review: $(/usr/bin/python3 -c "import json;print(len(json.load(open('$LOGS/../pending_review.json'))))" 2>/dev/null || echo '?') generated answers awaiting audit"
echo "runs today:     $(grep -c "$(/bin/date '+%Y-%m-%d')" "$HB")"
