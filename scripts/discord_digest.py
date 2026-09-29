#!/usr/bin/env python3
"""
discord_digest.py -- a daily email of what happened on the conference Discord.

Reports, for the last 24 hours: who joined, which channels saw activity, how
many messages each carried, and the text of any message the bot is allowed to
read.

A caveat the email states plainly rather than hiding. MESSAGE CONTENT INTENT is
currently OFF for this application in the Discord Developer Portal, so Discord
returns an empty `content` field for messages the bot did not write itself.
Measured 2026-09-23: of 14 human messages in the server, the bot could read 0.
Counts, authors, channels and timestamps are all still visible, so the digest is
useful as an activity report, but it cannot quote people. Turning the intent on
(Developer Portal -> Bot -> Privileged Gateway Intents -> MESSAGE CONTENT) makes
the text appear here with no change to this script.

Usage:
    python3 scripts/discord_digest.py            # print, do not send
    python3 scripts/discord_digest.py --send     # email it
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

GUILD = "1547593245809451119"
API = "https://discord.com/api/v10"
TOKEN_PATH = os.environ.get(
    "DISCORD_BOT_TOKEN_FILE",
    "/Users/louishyman/coding/discord_ai_conference_bot_token.txt")
TO = os.environ.get("DIGEST_TO", "lhyman6@jh.edu")
WINDOW_HOURS = 24
UA = "DiscordBot (https://proflouishyman.github.io/ai_conference_2026, 1.0)"


def token():
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def call(path, tok):
    req = urllib.request.Request(
        API + path, headers={"Authorization": "Bot " + tok, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def snowflake_time(sid):
    """Discord ids embed their creation time: (id >> 22) + Discord epoch."""
    return datetime.fromtimestamp(((int(sid) >> 22) + 1420070400000) / 1000, timezone.utc)


def gather(tok, since):
    me = call("/users/@me", tok)
    channels = [c for c in call(f"/guilds/{GUILD}/channels", tok) if c["type"] == 0]

    joins, activity, readable, blocked = [], [], [], 0
    for ch in channels:
        try:
            msgs = call(f"/channels/{ch['id']}/messages?limit=100", tok)
        except Exception:
            continue                      # no read access to this channel
        recent = [m for m in msgs
                  if datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")) >= since]
        if not recent:
            continue
        human = 0
        for m in recent:
            if m.get("type") == 7:        # join notification, not a message
                joins.append((m["author"].get("username", "?"), m["timestamp"]))
                continue
            if m["author"]["id"] == me["id"]:
                continue                  # the bot's own posts are not news
            human += 1
            if m.get("content"):
                readable.append((ch["name"], m["author"].get("username", "?"),
                                 m["content"].strip()))
            else:
                blocked += 1
        if human:
            activity.append((ch["name"], human))
    return joins, activity, readable, blocked, len(channels)


def render(joins, activity, readable, blocked, nchannels, since):
    L = []
    L.append(f"Discord activity, {since:%A %-d %B} to {datetime.now(timezone.utc):%-d %B %Y}")
    L.append("")

    if not joins and not activity:
        L.append("Nothing happened in the last 24 hours. No new members and no messages.")
        L.append("")
    else:
        if joins:
            L.append(f"**New members: {len(joins)}**")
            for name, _ in joins:
                L.append(f"  {name}")
            L.append("")
        if activity:
            total = sum(n for _, n in activity)
            L.append(f"**Messages: {total} across {len(activity)} channel(s)**")
            for name, n in sorted(activity, key=lambda a: -a[1]):
                L.append(f"  #{name}: {n}")
            L.append("")

    if readable:
        L.append("**What was said**")
        L.append("")
        for chan, who, text in readable:
            body = text if len(text) <= 400 else text[:400] + "..."
            L.append(f"#{chan} -- {who}:")
            L.append(f"  {body}")
            L.append("")

    if blocked:
        L.append(f"**{blocked} message(s) could not be read.** MESSAGE CONTENT INTENT is off "
                 "for this bot, so Discord withholds the text of anything the bot did not "
                 "write. The counts above are still accurate. To see the words, turn on "
                 "Developer Portal -> Bot -> Privileged Gateway Intents -> MESSAGE CONTENT.")
        L.append("")

    L.append(f"Server: {nchannels} text channels. https://discord.gg/5EqFvR7edK")
    return "\n".join(L)


def main():
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    tok = token()
    joins, activity, readable, blocked, nchannels = gather(tok, since)
    body = render(joins, activity, readable, blocked, nchannels, since)

    if "--send" not in sys.argv:
        print(body)
        return 0

    # Quiet days still send: silence from the bot is indistinguishable from a
    # broken cron, and knowing the server was quiet is itself the report.
    sys.path.insert(0, os.path.expanduser("~/coding/agora_media/scripts"))
    from send_digest_email import send            # proven SMTP path
    from meltwater_client import load_env

    env = load_env()
    addr, pw = env.get("GMAIL_ADDRESS"), env.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from agora_media/.env", file=sys.stderr)
        return 1
    n = len(joins)
    subject = (f"Discord: {n} new member(s), {sum(a[1] for a in activity)} message(s)"
               if (joins or activity) else "Discord: quiet today")
    send([a.strip() for a in TO.split(",")], subject, body, addr, pw)
    print(f"Sent to {TO}: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
