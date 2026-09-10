#!/usr/bin/env python3
"""Publish glossary.json to #jargon-for-historians as one alphabetical run.

The channel and the bot read from the same file, so they cannot drift apart.
Re-running replaces the whole channel contents rather than appending, which
keeps a single ordered A-Z sequence no matter how many terms get added later.

Usage:
    python3 publish_glossary.py            # dry run, prints what would post
    python3 publish_glossary.py --apply    # wipe and repost
"""
import argparse
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

TOKEN_PATH = "/Users/louishyman/coding/discord_ai_conference_bot_token.txt"
GUILD = "1547593245809451119"
CHANNEL = "jargon-for-historians"
HERE = os.path.dirname(os.path.abspath(__file__))
GLOSSARY = os.path.join(HERE, "glossary.json")
API = "https://discord.com/api/v10"
LIMIT = 1900

INTRO = """# Jargon for historians

**There is going to be a lot of jargon at this conference, and most of it is unnecessary.**

That is not a complaint about the speakers. It is what happens when a field borrows its vocabulary from another field. Computer science named these things for its own purposes, historians inherited the names, and now a perfectly ordinary idea arrives wearing a technical word that makes it sound harder than it is.

Almost every term below describes something you already understand. "Ground truth" means a correct answer to check against. "Corpus" means the pile of documents you are working with. "Abstraction" is what a finding aid does. You have been doing versions of this for your whole career.

**Every term here is taken from the program or asked for by someone here**, not from a generic list. Alphabetical, so scan for what you need.

**Nobody at this conference is entitled to make you feel stupid for asking what a word means.** If a session leaves you lost, that is worth saying, here or in #ask-anything."""

OUTRO = """**Missing a word? Just ask here.** Write "what is X?" or "define X" and you will get an answer, usually within a few minutes.

The terms above are written by the organisers. Anything else is drafted by an AI and **clearly marked as unchecked** until a person has reviewed it, which happens at intervals throughout the conference. If a flagged definition is wrong, say so, and you will be right often enough to be worth listening to.

Using an AI to explain AI jargon has an obvious irony, and given that hallucination is a session topic here, the labelling is the point. Nothing pretends to be verified when it is not.

No question is too basic. If one term tripped you up it has tripped up thirty other people who did not ask."""


def token():
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def call(method, path, tok, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bot {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AIHistoryConf-Glossary/1.0")
    for _ in range(4):
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read().decode()
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


def sortkey(term):
    t = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def render(gloss):
    """One entry per line, grouped under letter headings."""
    entries = sorted(gloss.values(), key=lambda v: sortkey(v["term"]))
    out, letter = [], None
    for e in entries:
        first = sortkey(e["term"])[:1].upper() or "#"
        if first != letter:
            letter = first
            out.append(f"## {letter}")
        line = f"**{e['term']}** — {e['definition']}"
        if e.get("link"):
            line += f" [More]({e['link']})"
        if e.get("session"):
            line += f" *({e['session']})*"
        out.append(line)
    return out


def chunk(blocks, limit=LIMIT):
    """Pack rendered blocks into messages under Discord's size cap, never
    splitting a definition and never orphaning a letter heading."""
    msgs, cur = [], ""
    for i, b in enumerate(blocks):
        # a heading must travel with at least one entry
        unit = b
        if b.startswith("## ") and i + 1 < len(blocks):
            unit = b + "\n\n" + blocks[i + 1]
        if b.startswith("## ") and i + 1 < len(blocks):
            if len(cur) + len(unit) + 2 > limit and cur:
                msgs.append(cur.rstrip())
                cur = ""
            cur += b + "\n\n"
            continue
        if len(cur) + len(b) + 2 > limit and cur:
            msgs.append(cur.rstrip())
            cur = ""
        cur += b + "\n\n"
    if cur.strip():
        msgs.append(cur.rstrip())
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(GLOSSARY) as fh:
        gloss = json.load(fh)

    parts = [INTRO] + chunk(render(gloss)) + [OUTRO]
    over = [p for p in parts if len(p) > 2000]
    print(f"{len(gloss)} terms -> {len(parts)} messages "
          f"(largest {max(len(p) for p in parts)} chars)")
    if over:
        raise SystemExit(f"{len(over)} message(s) over the 2000 limit")

    if not args.apply:
        for i, p in enumerate(parts, 1):
            head = p.split("\n")[0][:60]
            print(f"  {i:>2}. {len(p):>5} chars  {head}")
        print("\nDRY RUN. Re-run with --apply to replace the channel.")
        return

    tok = token()
    chans = call("GET", f"/guilds/{GUILD}/channels", tok)
    ch = next((c for c in chans if c["name"] == CHANNEL and c["type"] == 0),
              None)
    if not ch:
        raise SystemExit(f"#{CHANNEL} not found")

    # clear existing messages so the A-Z stays one clean run
    while True:
        msgs = call("GET", f"/channels/{ch['id']}/messages?limit=100", tok)
        if not isinstance(msgs, list) or not msgs:
            break
        for m in msgs:
            call("DELETE", f"/channels/{ch['id']}/messages/{m['id']}", tok)
            time.sleep(0.35)
        if len(msgs) < 100:
            break

    first = None
    for i, p in enumerate(parts, 1):
        res = call("POST", f"/channels/{ch['id']}/messages", tok,
                   {"content": p})
        ok = "ERROR" not in res
        print(f"  posted {i}/{len(parts)}  {len(p)} chars  "
              f"{'ok' if ok else res}")
        if ok and first is None:
            first = res["id"]
        time.sleep(0.8)
    if first:
        call("PUT", f"/channels/{ch['id']}/pins/{first}", tok)
        time.sleep(1.0)
        # pinning posts a system message; clear it so the A-Z ends cleanly
        for m in call("GET", f"/channels/{ch['id']}/messages?limit=5", tok):
            if not m.get("content", "").strip() and not m.get("attachments"):
                call("DELETE", f"/channels/{ch['id']}/messages/{m['id']}", tok)
    print("\nPublished.")


if __name__ == "__main__":
    main()
