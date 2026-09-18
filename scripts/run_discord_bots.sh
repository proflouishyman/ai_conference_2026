#!/bin/zsh
# Runs both Discord bots. Called from cron, which has a minimal environment,
# so the OpenAI key is read from ~/.claude/settings.json rather than inherited.
cd /Users/louishyman/coding/ai_conference_2026 || exit 1

KEY=$(/usr/bin/python3 -c "import json;print(json.load(open('/Users/louishyman/.claude/settings.json')).get('env',{}).get('OPENAI_API_KEY',''))" 2>/dev/null)
[ -n "$KEY" ] && export OPENAI_API_KEY="$KEY"

/usr/bin/python3 scripts/discord_jargon_bot.py --apply  >> scripts/logs/jargon_bot.log  2>&1
/usr/bin/python3 scripts/discord_channel_bot.py --apply >> scripts/logs/channel_bot.log 2>&1
