#!/usr/bin/env python3
"""Answer jargon questions in #jargon-for-historians.

Two tiers:
  1. glossary.json      -- human-written, posted as authoritative.
  2. LLM-generated      -- posted immediately, clearly flagged as unreviewed,
                           and queued in pending_review.json for a human.

Every generated definition is written to pending_review.json with the question,
the answer, the Discord message id and a permalink, so it can be audited on an
interval. Approving one promotes it into glossary.json (where it is served with
no warning label from then on); rejecting one edits or deletes the posted
message.

Usage
-----
    python3 discord_jargon_bot.py                 # dry run
    python3 discord_jargon_bot.py --apply         # answer new questions
    python3 discord_jargon_bot.py --review        # list what needs auditing
    python3 discord_jargon_bot.py --approve <id>  # promote to the glossary
    python3 discord_jargon_bot.py --reject <id> --reason "..."   # retract

Cron (every 5 min during the conference):
    */5 * * * * cd ~/coding/ai_conference_2026 && \
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
PENDING = os.path.join(HERE, "pending_review.json")
API = "https://discord.com/api/v10"

MAX_GENERATED_PER_RUN = 6

ASK = re.compile(
    r"(?:^|\s)(?:what(?:'?s| is| are| does)\s+(?:an?\s+)?|"
    r"define\s+|meaning of\s+|what do(?:es)? .* mean by\s+|\?)"
    r"([A-Za-z][A-Za-z0-9 \-/]{1,40})", re.I)

FLAG = ("\n\n*Machine-generated and not yet checked by a person. "
        "It will be reviewed and corrected if wrong. If you know better, "
        "please say so.*")

SYSTEM = """You define technical terms for historians attending a conference \
on AI and computational methods.

Your audience are professional historians, archivists and librarians. They are \
intellectually sophisticated but most have no computer science background. Two \
thirds describe themselves as new to computational methods.

Rules:
- Two to four sentences. No longer.
- Plain English. If you must use another technical term, define it in passing.
- Lead with what the thing IS, not its history or why it matters.
- Where the concept maps onto something historians already do, say so.
- Never condescend and never pad.
- No markdown headers, no bullet lists. Flowing prose.
- If a term has a specific meaning in this field that differs from ordinary \
usage, say so explicitly. "Model" is the worst offender.
- If you are not confident what the term means in this context, say so plainly \
rather than guessing. An honest "this could mean two things" is far better \
than a confident wrong answer.
"""


def token():
    with open(TOKEN_PATH) as fh:
        return fh.read().strip()


def call(method, path, tok, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bot {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AIHistoryConf-JargonBot/2.0")
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
    t = re.sub(r"[^a-z0-9 \-/]", "", term.strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


def lookup(term, gloss):
    n = normalise(term)
    if not n:
        return None, None
    if n in gloss:
        return n, gloss[n]
    for key, val in gloss.items():
        if n in [normalise(a) for a in val.get("aliases", [])]:
            return key, val
    for key, val in gloss.items():
        if len(n) > 3 and (n in key or key in n):
            return key, val
    return None, None


def generate(term, context):
    """Ask an LLM for a definition. Returns (text, model) or (None, None)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None, None
    try:
        from openai import OpenAI
    except ImportError:
        return None, None
    prompt = (f'Define "{term}" for this audience.')
    if context and len(context) < 400:
        prompt += f'\n\nThey asked: "{context.strip()}"'
    try:
        client = OpenAI(api_key=key)
        model = "gpt-4o"
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": prompt}],
            max_tokens=320,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip(), model
    except Exception as exc:
        print(f"  ! LLM call failed: {str(exc)[:160]}")
        return None, None


# ------------------------------------------------------------------ review

def cmd_review(pending):
    unreviewed = {k: v for k, v in pending.items() if not v.get("reviewed")}
    if not unreviewed:
        print("Nothing awaiting review.")
        return
    print(f"{len(unreviewed)} generated definition(s) awaiting review:\n")
    for pid, p in sorted(unreviewed.items(), key=lambda x: x[1]["at"]):
        print(f"--- {pid} --- ({p['at']})")
        print(f"  asked:  {p['question'][:90]}")
        print(f"  term:   {p['term']}")
        print(f"  answer: {p['answer'][:400]}")
        print(f"  link:   {p.get('permalink','')}\n")
    print("Approve:  --approve <id>      (adds to glossary.json, drops the "
          "unreviewed flag on the posted message)")
    print("Reject:   --reject <id> --reason \"...\"")


def cmd_approve(pid, pending, gloss, tok):
    p = pending.get(pid)
    if not p:
        raise SystemExit(f"No pending item {pid}")
    key = normalise(p["term"])
    gloss[key] = {"term": p["term"].strip().title(),
                  "definition": p["answer"].replace(FLAG, "").strip(),
                  "reviewed_by": "human", "source": "llm-approved"}
    save(GLOSSARY, gloss)
    clean = p["answer"].replace(FLAG, "")
    if p.get("message_id"):
        call("PATCH", f"/channels/{p['channel_id']}/messages/{p['message_id']}",
             tok, {"content": clean})
    p["reviewed"] = True
    p["outcome"] = "approved"
    save(PENDING, pending)
    print(f"Approved '{p['term']}' into the glossary and removed the flag.")


def cmd_reject(pid, reason, pending, tok):
    p = pending.get(pid)
    if not p:
        raise SystemExit(f"No pending item {pid}")
    note = (f"**Correction.** The definition posted here was machine-generated "
            f"and wrong. {reason.strip()}")
    if p.get("message_id"):
        call("PATCH", f"/channels/{p['channel_id']}/messages/{p['message_id']}",
             tok, {"content": note})
    p["reviewed"] = True
    p["outcome"] = "rejected"
    p["correction"] = reason
    save(PENDING, pending)
    print(f"Retracted the definition of '{p['term']}' and posted a correction.")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--approve")
    ap.add_argument("--reject")
    ap.add_argument("--reason", default="")
    ap.add_argument("--no-llm", action="store_true",
                    help="glossary only, do not generate")
    args = ap.parse_args()

    tok = token()
    gloss = load(GLOSSARY, {})
    pending = load(PENDING, {})

    if args.review:
        return cmd_review(pending)
    if args.approve:
        return cmd_approve(args.approve, pending, gloss, tok)
    if args.reject:
        if not args.reason:
            raise SystemExit("--reject needs --reason explaining what was wrong")
        return cmd_reject(args.reject, args.reason, pending, tok)

    st = load(STATE, {"handled": []})
    handled = set(st["handled"])

    chans = call("GET", f"/guilds/{GUILD}/channels", tok)
    ch = next((c for c in chans
               if c["name"] == CHANNEL_NAME and c["type"] == 0), None)
    if not ch:
        raise SystemExit(f"#{CHANNEL_NAME} not found")

    msgs = call("GET", f"/channels/{ch['id']}/messages?limit=50", tok)
    if isinstance(msgs, dict):
        raise SystemExit("Could not read channel")

    generated = 0
    for m in reversed(msgs):
        if m["id"] in handled or m["author"].get("bot"):
            continue
        hit = ASK.search(m["content"] or "")
        if not hit:
            continue
        asked = hit.group(1).strip()
        _, entry = lookup(asked, gloss)

        source = None
        if entry:
            body = f"**{entry['term']}** — {entry['definition']}"
            if entry.get("link"):
                body += f"\n{entry['link']}"
            if entry.get("session"):
                body += f"\n*Comes up in: {entry['session']}*"
            source = "glossary"
            print(f"+ glossary: {asked}")
        elif args.no_llm:
            continue
        else:
            if generated >= MAX_GENERATED_PER_RUN:
                print("- hit per-run generation cap")
                break
            text, model = generate(asked, m["content"])
            if not text:
                continue
            body = f"**{asked.strip().title()}** — {text}{FLAG}"
            source = "llm"
            generated += 1
            print(f"* generated: {asked}  ({model})")

        if not args.apply:
            continue

        res = call("POST", f"/channels/{ch['id']}/messages", tok,
                   {"content": body[:2000],
                    "message_reference": {"message_id": m["id"]}})
        handled.add(m["id"])
        if source == "llm" and "id" in res:
            pending[res["id"]] = {
                "term": asked,
                "question": m["content"][:300],
                "answer": body,
                "model": model,
                "channel_id": ch["id"],
                "message_id": res["id"],
                "permalink": f"https://discord.com/channels/{GUILD}/"
                             f"{ch['id']}/{res['id']}",
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "reviewed": False,
            }
        time.sleep(0.6)

    if args.apply:
        st["handled"] = sorted(handled)[-500:]
        st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(STATE, st)
        save(PENDING, pending)

    awaiting = sum(1 for v in pending.values() if not v.get("reviewed"))
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {generated} generated, "
          f"{len(gloss)} in glossary, {awaiting} awaiting review")


if __name__ == "__main__":
    main()
