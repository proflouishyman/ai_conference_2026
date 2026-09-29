#!/usr/bin/env python3
"""Build the AI and History Conference 2026 Discord server structure.

Creates categories, channels, topics and roles per
plans/2026-09-09_1745_discord-build-spec.md (field axis dropped 2026-09-10).

Idempotent: re-running skips anything that already exists by name, so a partial
run can be repeated safely. It never deletes anything.

Usage:
    python3 build_discord.py --list-guilds     # show servers the bot is in
    python3 build_discord.py --guild <id>      # dry run, prints the plan
    python3 build_discord.py --guild <id> --apply
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error

TOKEN_PATH = "/Users/louishyman/coding/discord_ai_conference_bot_token.txt"
API = "https://discord.com/api/v10"

# ---------------------------------------------------------------- structure

# (name, topic)
START_HERE = [
    ("welcome",
     "What this is and how it works. Read-only. Code of conduct lives here."),
    ("introductions",
     "One line about who you are and what you work on. Best place to start if "
     "you are not sure which channels are for you."),
    ("ask-anything",
     "Beginner-first. No question is too basic here. Staffed during the "
     "conference. If a channel feels over your head, bring the question here."),
    ("live-sessions",
     "Discussion of the streamed sessions. One thread per session."),
    ("hallway",
     "Unstructured. The corridor conversation between sessions."),
    ("whats-everyone-working-on",
     "Rolling thread. Say what you are working on right now, however early."),
]

# (name, topic, online_count, beginner_pct)
TECHNOLOGY = [
    ("llms", "Large language models for research tasks.", 169, 57),
    ("ocr-and-transcription", "OCR and handwritten text recognition.", 117, 48),
    ("data-visualization", "Charts, graphs and visual presentation of historical data.", 83, 32),
    ("maps-and-gis", "GIS, mapping and spatial history.", 77, 44),
    ("databases", "Building and managing research databases.", 71, 42),
    ("text-mining-nlp", "Text mining and natural language processing.", 65, 23),
    ("network-analysis", "Network analysis of historical relationships.", 52, 18),
    ("machine-learning-cv", "Machine learning and computer vision.", 26, 21),
]

# (name, topic)
PLACE = [
    ("baltimore-dc",
     "Maryland, DC, Virginia, Pennsylvania, Delaware, New Jersey, West "
     "Virginia. The largest local group by a wide margin."),
    ("international",
     "Everyone outside the US. UK, Canada, India, Australia and more. Post "
     "across time zones, nobody expects an instant reply."),
    ("midwest-and-west",
     "Midwest, West Coast and Mountain West."),
    ("northeast",
     "New England, New York and Boston."),
    ("south",
     "The US South."),
]

ROLES = [
    # (name, colour, hoist, permissions)
    ("Organiser", 0xC0392B, True, "8"),           # Administrator
    ("Speaker",   0x2980B9, True, "0"),
    ("Volunteer", 0x27AE60, True, "0"),
]


def level_note(beginner_pct):
    if beginner_pct >= 45:
        return ("Skews beginner: most people here are getting started, so "
                "basic questions are normal.")
    if beginner_pct >= 30:
        return "Mixed levels, beginners welcome."
    return ("Skews experienced. New to this? #ask-anything is a better "
            "starting point.")


# ---------------------------------------------------------------- api helpers

def read_token():
    try:
        with open(TOKEN_PATH) as fh:
            tok = fh.read().strip()
    except OSError as exc:
        sys.exit(f"Cannot read token: {exc}")
    if not tok:
        sys.exit("Token file is empty.")
    return tok


def call(method, path, token, payload=None, retry=5):
    url = API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AIHistoryConf/1.0")
    for attempt in range(retry):
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            if exc.code == 429:  # rate limited
                try:
                    wait = float(json.loads(body).get("retry_after", 1.5))
                except Exception:
                    wait = 1.5
                time.sleep(wait + 0.4)
                continue
            raise SystemExit(f"HTTP {exc.code} on {method} {path}\n{body}")
        except urllib.error.URLError as exc:
            if attempt == retry - 1:
                raise SystemExit(f"Network error on {method} {path}: {exc}")
            time.sleep(2)
    raise SystemExit(f"Gave up after {retry} attempts: {method} {path}")


# ---------------------------------------------------------------- operations

def list_guilds(token):
    me = call("GET", "/users/@me", token)
    print(f"Bot: {me.get('username')}#{me.get('discriminator')} (id {me.get('id')})")
    guilds = call("GET", "/users/@me/guilds", token)
    if not guilds:
        print("\nBot is not in any server yet. Open the OAuth2 invite URL and "
              "authorize it, then run this again.")
        return
    print("\nServers this bot can see:")
    for g in guilds:
        print(f"  {g['id']}  {g['name']}")


def build(token, guild_id, apply):
    guild = call("GET", f"/guilds/{guild_id}", token)
    print(f"Server: {guild['name']} (id {guild_id})")

    existing_ch = call("GET", f"/guilds/{guild_id}/channels", token)
    by_name = {c["name"].lower(): c for c in existing_ch}
    existing_roles = {r["name"].lower(): r
                      for r in call("GET", f"/guilds/{guild_id}/roles", token)}

    plan = [
        ("START HERE", [(n, t) for n, t in START_HERE]),
        ("TECHNOLOGY", [(n, f"{t} {level_note(b)} ({c} online registrants)")
                        for n, t, c, b in TECHNOLOGY]),
        ("PLACE", [(n, t) for n, t in PLACE]),
    ]

    created = skipped = 0

    for cat_name, channels in plan:
        cat = by_name.get(cat_name.lower())
        if cat:
            print(f"\n[=] category exists: {cat_name}")
            cat_id = cat["id"]
        elif apply:
            cat = call("POST", f"/guilds/{guild_id}/channels", token,
                       {"name": cat_name, "type": 4})
            cat_id = cat["id"]
            print(f"\n[+] category: {cat_name}")
            time.sleep(0.4)
        else:
            cat_id = None
            print(f"\n[+] category: {cat_name}   (dry run)")

        for name, topic in channels:
            if name.lower() in by_name:
                print(f"    [=] #{name}")
                skipped += 1
                continue
            if apply:
                body = {"name": name, "type": 0, "topic": topic[:1024]}
                if cat_id:
                    body["parent_id"] = cat_id
                call("POST", f"/guilds/{guild_id}/channels", token, body)
                time.sleep(0.4)
            print(f"    [+] #{name}")
            created += 1

    print("\nRoles:")
    for rname, colour, hoist, perms in ROLES:
        if rname.lower() in existing_roles:
            print(f"    [=] {rname}")
            continue
        if apply:
            call("POST", f"/guilds/{guild_id}/roles", token,
                 {"name": rname, "color": colour, "hoist": hoist,
                  "permissions": perms, "mentionable": False})
            time.sleep(0.4)
        print(f"    [+] {rname}")

    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: "
          f"{created} channels to create, {skipped} already present.")
    if not apply:
        print("Re-run with --apply to make these changes.")
    else:
        print("\nStill to do by hand (no API for these):")
        print("  - Onboarding questions (Server Settings > Onboarding)")
        print("  - Verification level: Medium")
        print("  - Disable @everyone for the default role")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guild")
    ap.add_argument("--list-guilds", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    token = read_token()
    if args.list_guilds or not args.guild:
        list_guilds(token)
        if not args.guild:
            return
    build(token, args.guild, args.apply)


if __name__ == "__main__":
    main()
