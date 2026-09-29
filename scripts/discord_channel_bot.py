#!/usr/bin/env python3
"""Auto-create Discord channels from votes in #suggest-a-channel.

How it works
------------
Someone posts a suggestion in #suggest-a-channel. People react with an emoji.
When a suggestion reaches THRESHOLD distinct reactors, this script creates the
channel, replies in-thread with the result, and marks the request done.

This POLLS the REST API rather than holding a gateway connection, so it needs
no privileged Message Content intent and can run from cron. Run it as often as
you like; it is idempotent and keeps its own state file.

Usage
-----
    python3 discord_channel_bot.py            # dry run, shows what it would do
    python3 discord_channel_bot.py --apply    # actually create channels
    python3 discord_channel_bot.py --apply --threshold 3

Cron (every 15 min):
    */15 * * * * cd ~/coding/ai_conference_2026 && \
      /usr/bin/python3 scripts/discord_channel_bot.py --apply >> \
      scripts/logs/channel_bot.log 2>&1
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

TOKEN_PATH = "/Users/louishyman/coding/discord_ai_conference_bot_token.txt"
GUILD = "1547593245809451119"
SUGGEST_CHANNEL = "suggest-a-channel"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "discord_channel_bot_state.json")
API = "https://discord.com/api/v10"

DEFAULT_THRESHOLD = 3          # distinct reactors needed
MAX_NEW_PER_RUN = 5            # safety brake
VOTE_EMOJI = "\N{THUMBS UP SIGN}"

# Never auto-create anything matching these — reserved, risky, or nonsense.
BLOCKED = re.compile(
    r"(admin|mod|staff|owner|announce|rule|everyone|here|nsfw|porn|sex|"
    r"nazi|hitler|kill|suicide|drug)", re.I)

CATEGORY_NAME = "MEMBER CHANNELS"


def token():
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def call(method, path, tok, payload=None, retry=4):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bot {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AIHistoryConf-ChannelBot/1.0")
    for attempt in range(retry):
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if exc.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 1.5))
                except Exception:
                    wait = 1.5
                time.sleep(wait + 0.4)
                continue
            print(f"  ! HTTP {exc.code} {method} {path}: {body[:200]}")
            return {"ERROR": exc.code}
        except urllib.error.URLError as exc:
            if attempt == retry - 1:
                print(f"  ! network error: {exc}")
                return {"ERROR": "network"}
            time.sleep(2)
    return {"ERROR": "retries"}


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"handled": []}


def save_state(st):
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=1)


def slugify(text):
    """First line of a suggestion -> a discord channel name."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    # strip a leading "channel:" / "please add" style preamble
    line = re.sub(r"^\s*(can we (have|get)|please (add|make)|suggestion|"
                  r"channel|request)\s*[:\-]?\s*", "", line, flags=re.I)
    line = re.sub(r"^#", "", line.strip())
    slug = re.sub(r"[^a-z0-9]+", "-", line.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:90]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()

    tok = token()
    st = load_state()
    handled = set(st.get("handled", []))

    channels = call("GET", f"/guilds/{GUILD}/channels", tok)
    if isinstance(channels, dict) and "ERROR" in channels:
        sys.exit("Cannot list channels.")

    existing = {c["name"].lower() for c in channels}
    suggest = next((c for c in channels
                    if c["name"] == SUGGEST_CHANNEL and c["type"] == 0), None)
    if not suggest:
        sys.exit(f"#{SUGGEST_CHANNEL} not found.")

    category = next((c["id"] for c in channels
                     if c["type"] == 4 and c["name"] == CATEGORY_NAME), None)

    msgs = call("GET", f"/channels/{suggest['id']}/messages?limit=100", tok)
    if isinstance(msgs, dict) and "ERROR" in msgs:
        sys.exit("Cannot read suggestions.")

    bot = call("GET", "/users/@me", tok)
    created = 0

    for m in msgs:
        mid = m["id"]
        if mid in handled:
            continue
        if m["author"].get("bot"):
            continue

        votes = 0
        for r in m.get("reactions", []):
            if r["emoji"]["name"] == VOTE_EMOJI:
                votes = r["count"]
                break
        if votes < args.threshold:
            continue

        slug = slugify(m["content"])
        preview = m["content"].strip().splitlines()[0][:60] if m["content"] else "(empty)"

        if not slug:
            print(f"- skip (no usable name): {preview}")
            continue
        if BLOCKED.search(slug):
            print(f"- BLOCKED name '{slug}' — needs a human: {preview}")
            continue
        if slug in existing:
            print(f"- exists already: #{slug}")
            handled.add(mid)
            continue
        if created >= MAX_NEW_PER_RUN:
            print(f"- hit per-run cap ({MAX_NEW_PER_RUN}), leaving the rest")
            break

        print(f"+ #{slug}  ({votes} votes)  from: {preview}")
        if args.apply:
            body = {"name": slug, "type": 0,
                    "topic": f"Requested by members in #{SUGGEST_CHANNEL}."}
            if category:
                body["parent_id"] = category
            res = call("POST", f"/guilds/{GUILD}/channels", tok, body)
            if "ERROR" in res:
                continue
            time.sleep(0.5)
            call("POST", f"/channels/{suggest['id']}/messages", tok,
                 {"content": f"Made it: <#{res['id']}>. "
                             f"{votes} people wanted this one.",
                  "message_reference": {"message_id": mid}})
            time.sleep(0.5)
            existing.add(slug)
            handled.add(mid)
        created += 1

    if args.apply:
        st["handled"] = sorted(handled)
        st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(st)

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {created} channel(s), "
          f"threshold {args.threshold}, bot {bot.get('username')}")
    if not args.apply and created:
        print("Re-run with --apply to create them.")


if __name__ == "__main__":
    main()
