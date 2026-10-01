#!/usr/bin/env python3
"""Keep glossary.json and the #jargon-for-historians A-Z master list in step.

Used by discord_bot_loop.py (adds bot-drafted terms) and discord_jargon_bot.py
(--approve / --reject). Everything that writes glossary.json or
pending_review.json goes through locked_update(), so the loop and a manual
--approve run on the same machine cannot lose each other's writes.

Privacy: entries hold only the term, the definition, a permalink made of
Discord IDs, and provenance flags. No usernames, no asker names, no question
text.
"""
import fcntl
import json
import os
import re
import tempfile
import time

from publish_glossary import MARK, chunk, render

HERE = os.path.dirname(os.path.abspath(__file__))
GLOSSARY = os.path.join(HERE, "glossary.json")
MAX_DEF = 1200
MIN_REFRESH_S = 60
INTRO_HEAD = "# Jargon for historians"
OUTRO_HEAD = "**Missing a word?"

_FOOTER = re.compile(r"\n*\s*\*(?:AI-generated|Machine-generated)[^\n]*\*\s*$",
                     re.S)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def normalise(term):
    t = re.sub(r"[^a-z0-9 \-/]", "", term.strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


# ------------------------------------------------------------ safe file I/O
def locked_update(path, fn, default=None):
    """Read-modify-write under an exclusive flock, written atomically
    (temp file, fsync, rename). fn(obj) mutates obj in place and returns a
    truthy value if it changed anything. Returns fn's result."""
    default = {} if default is None else default
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                obj = json.load(fh)
        except (FileNotFoundError, ValueError):
            obj = default
        res = fn(obj)
        if res:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                       suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(obj, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        return res


def load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {} if default is None else default


# ------------------------------------------------------------ glossary edits
def clean_definition(text, term=""):
    """Strip the AI footer and any leading '**Term**:' prefix."""
    t = _FOOTER.sub("", text).strip()
    t = re.sub(r"^\*\*[^*\n]{1,80}\*\*\s*[:—-]\s*", "", t)
    if len(t) > MAX_DEF:
        cut = t[:MAX_DEF]
        dot = max(cut.rfind(". "), cut.rfind("? "))
        t = cut[:dot + 1] if dot > MAX_DEF // 2 else cut.rstrip() + "…"
    return t


def add_bot_term(term, reply_text, question_permalink, asked="",
                 path=GLOSSARY, today=None):
    """Add a bot-drafted entry. Returns (key, entry) if added, else
    (None, reason). Never overwrites or shadows an existing entry."""
    key = normalise(term)
    if not key:
        return None, "empty term"
    definition = clean_definition(reply_text, term)
    if len(definition) < 20:
        return None, "definition too short"
    if _EMAIL.search(definition) or "<@" in definition:
        return None, "looks like it contains an address or mention"
    alias = normalise(asked) if asked else ""
    out = {}

    def fn(g):
        names = set()
        for k, v in g.items():
            names.add(k)
            names.update(normalise(a) for a in v.get("aliases", []))
        if key in names:
            out["why"] = "already in glossary"
            return False
        entry = {"term": term.strip(), "definition": definition,
                 "source": "bot",
                 "added": today or time.strftime("%Y-%m-%d"),
                 "question_permalink": question_permalink,
                 "reviewed": False}
        if alias and alias != key and alias not in names:
            entry["aliases"] = [alias]
        g[key] = entry
        out["entry"] = entry
        return True

    if locked_update(path, fn):
        return key, out["entry"]
    return None, out["why"]


def remove_bot_term(term, path=GLOSSARY):
    """Remove a bot-sourced entry only. Human entries are never touched."""
    key = normalise(term)

    def fn(g):
        if key in g and g[key].get("source") == "bot":
            del g[key]
            return True
        return False
    return bool(locked_update(path, fn))


def mark_reviewed(term, path=GLOSSARY):
    key = normalise(term)

    def fn(g):
        e = g.get(key)
        if e and e.get("source") == "bot" and not e.get("reviewed"):
            e["reviewed"] = True
            e["reviewed_by"] = "human"
            return True
        return False
    return bool(locked_update(path, fn))


# ------------------------------------------------------------ master list
def _is_az(m):
    c = m.get("content", "")
    return c.startswith("## ") or bool(
        re.match(r"\*\*[^*\n]+\*\*(?: \*\(AI-drafted\)\*)? — ", c))


def find_run(msgs, me_id):
    """-> (intro, [az messages in order], outro) from the bot's own messages,
    or None if the published run cannot be identified."""
    mine = sorted((m for m in msgs if m.get("author", {}).get("id") == me_id),
                  key=lambda m: int(m["id"]))
    intro = next((m for m in mine if m["content"].startswith(INTRO_HEAD)), None)
    outros = [m for m in mine if m["content"].startswith(OUTRO_HEAD)]
    if not intro or not outros:
        return None
    outro = outros[-1]
    az = [m for m in mine if int(intro["id"]) < int(m["id"]) <= int(outro["id"])
          and m is not outro and _is_az(m)]
    if not az:
        return None
    return intro, az, outro


def plan(gloss, intro, az, outro):
    """Ordered ops that bring the channel to the desired A-Z run:
    ("PATCH", id, content) / ("POST", None, content) / ("DELETE", id, None).
    Existing messages are reused in order; on overflow the old closing note is
    rewritten as the extra A-Z page and the closing note re-posted at the end
    (one POST, order preserved)."""
    want = chunk(render(gloss))
    bad = [c for c in want if len(c) > 2000]
    if bad:
        raise ValueError(f"{len(bad)} A-Z page(s) over 2000 chars")
    ops = []
    for i, content in enumerate(want):
        if i < len(az):
            if az[i]["content"] != content:
                ops.append(("PATCH", az[i]["id"], content))
        elif i == len(az):                      # overflow: borrow the outro slot
            ops.append(("PATCH", outro["id"], content))
            ops.append(("POST", None, outro["content"]))
        else:
            ops.append(("POST", None, content))
    for extra in az[len(want):]:                # shrank (a rejected term)
        ops.append(("DELETE", extra["id"], None))
    if len(want) > len(az):                     # outro re-posted last, keep order
        ops.sort(key=lambda o: o[0] == "POST" and o[2] == outro["content"])
    return ops


def refresh_master(call, channel_id, me_id, gloss, apply=True, out=print):
    """Bring the A-Z run in line with `gloss`. `call(method, path, payload)`
    must raise on failure. Returns the planned ops."""
    msgs = call("GET", f"/channels/{channel_id}/messages?limit=100", None)
    run = find_run(msgs, me_id)
    if not run:
        raise RuntimeError("A-Z run not found in channel; refusing to touch it")
    ops = plan(gloss, *run)
    for method, mid, content in ops:
        out(f"  {method:6} {mid or '(new)':>20}  "
            + (f"{len(content)} chars, starts {content[:50]!r}" if content else ""))
        if not apply:
            continue
        if method == "PATCH":
            call("PATCH", f"/channels/{channel_id}/messages/{mid}",
                 {"content": content, "allowed_mentions": {"parse": []}})
        elif method == "POST":
            call("POST", f"/channels/{channel_id}/messages",
                 {"content": content, "allowed_mentions": {"parse": []}})
        else:
            call("DELETE", f"/channels/{channel_id}/messages/{mid}", None)
    return ops


class Refresher:
    """Batches new terms and allows at most one refresh per MIN_REFRESH_S."""

    def __init__(self, min_s=MIN_REFRESH_S, clock=time.monotonic):
        self.min_s, self.clock = min_s, clock
        self.dirty = False
        self.last = None

    def mark(self):
        self.dirty = True

    def due(self):
        return self.dirty and (self.last is None
                               or self.clock() - self.last >= self.min_s)

    def done(self):
        self.dirty = False
        self.last = self.clock()
