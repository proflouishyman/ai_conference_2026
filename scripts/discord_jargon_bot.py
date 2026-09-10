#!/usr/bin/env python3
"""Answer jargon questions in #jargon-for-historians from a local glossary.

Watches the channel for questions ("what is X?", "define X", "?X") and replies
with the definition if the term is in glossary.json. Unknown terms are logged
to unknown_terms.json so a human can add them, and the asker is told a person
will pick it up.

Deliberately NOT wired to a language model. This channel exists because people
are unsure what words mean, and a confidently wrong auto-generated definition
is worse than no definition. Every answer here is one a human wrote.

Usage:
    python3 discord_jargon_bot.py              # dry run
    python3 discord_jargon_bot.py --apply
    python3 discord_jargon_bot.py --unknown    # list terms nobody has defined

Cron (every 10 min):
    */10 * * * * cd ~/coding/ai_conference_2026 && \
      /usr/bin/python3 scripts/discord_jargon_bot.py --apply >> \
      scripts/logs/jargon_bot.log 2>&1
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request

TOKEN_PATH = "/Users/louishyman/coding/discord_ai_conference_bot_token.txt"
GUILD = "1547593245809451119"
CHANNEL_NAME = "jargon-for-historians"
HERE = os.path.dirname(os.path.abspath(__file__))
GLOSSARY = os.path.join(HERE, "glossary.json")
STATE = os.path.join(HERE, "discord_jargon_bot_state.json")
UNKNOWN = os.path.join(HERE, "unknown_terms.json")
API = "https://discord.com/api/v10"

ASK = re.compile(
    r"(?:^|\s)(?:what(?:'?s| is| are| does)\s+(?:an?\s+)?|"
    r"define\s+|meaning of\s+|what do(?:es)? .* mean by\s+|\?)"
    r"([A-Za-z][A-Za-z0-9 \-/]{1,40})", re.I)


def token():
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def call(method, path, tok, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bot {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AIHistoryConf-JargonBot/1.0")
    for _ in range(4):
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
            return {"ERROR": exc.code, "body": body[:200]}
        except urllib.error.URLError:
            time.sleep(2)
    return {"ERROR": "retries"}


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)


def normalise(term):
    t = term.strip().lower()
    t = re.sub(r"[^a-z0-9 \-/]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(the|a|an) ", "", t)
    return t


def lookup(term, gloss):
    n = normalise(term)
    if not n:
        return None, None
    if n in gloss:
        return n, gloss[n]
    for key, val in gloss.items():           # alias match
        if n in [normalise(a) for a in val.get("aliases", [])]:
            return key, val
    for key, val in gloss.items():           # last resort: containment
        if len(n) > 3 and (n in key or key in n):
            return key, val
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unknown", action="store_true")
    args = ap.parse_args()

    if args.unknown:
        unk = load(UNKNOWN, {})
        if not unk:
            print("No undefined terms outstanding.")
            return
        print("Terms asked about but not in the glossary:\n")
        for term, meta in sorted(unk.items(), key=lambda x: -x[1]["count"]):
            print(f"  {meta['count']:>3}x  {term}")
        print(f"\nAdd them to {GLOSSARY} and they answer automatically.")
        return

    tok = token()
    gloss = load(GLOSSARY, {})
    if not gloss:
        raise SystemExit(f"No glossary at {GLOSSARY}")
    st = load(STATE, {"handled": []})
    handled = set(st["handled"])
    unknown = load(UNKNOWN, {})

    chans = call("GET", f"/guilds/{GUILD}/channels", tok)
    ch = next((c for c in chans
               if c["name"] == CHANNEL_NAME and c["type"] == 0), None)
    if not ch:
        raise SystemExit(f"#{CHANNEL_NAME} not found")

    msgs = call("GET", f"/channels/{ch['id']}/messages?limit=50", tok)
    if isinstance(msgs, dict):
        raise SystemExit("Could not read channel")

    answered = 0
    for m in reversed(msgs):
        if m["id"] in handled or m["author"].get("bot"):
            continue
        hit = ASK.search(m["content"] or "")
        if not hit:
            continue
        asked = hit.group(1).strip()
        key, entry = lookup(asked, gloss)

        if entry:
            body = f"**{entry['term']}** — {entry['definition']}"
            if entry.get("link"):
                body += f"\n{entry['link']}"
            if entry.get("session"):
                body += f"\n*Comes up in: {entry['session']}*"
            print(f"+ answering '{asked}' -> {key}")
        else:
            n = normalise(asked)
            unknown.setdefault(n, {"count": 0, "first_seen": m["timestamp"]})
            unknown[n]["count"] += 1
            body = (f"I do not have a definition for **{asked}** yet. "
                    f"Flagged for a human to write one, and it will be added "
                    f"to this channel. In the meantime, someone here may well "
                    f"know.")
            print(f"? unknown '{asked}' (logged)")

        if args.apply:
            call("POST", f"/channels/{ch['id']}/messages", tok,
                 {"content": body,
                  "message_reference": {"message_id": m["id"]}})
            handled.add(m["id"])
            time.sleep(0.6)
        answered += 1

    if args.apply:
        st["handled"] = sorted(handled)[-500:]
        st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(STATE, st)
        save(UNKNOWN, unknown)

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {answered} question(s), "
          f"{len(gloss)} terms in glossary, {len(unknown)} undefined")


if __name__ == "__main__":
    main()
