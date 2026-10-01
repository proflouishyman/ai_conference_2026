#!/usr/bin/env python3
"""Registration gate for the AI and History Conference 2026 Discord.

New members get a DM, reply with the email they registered with, receive a
6-digit code by email, reply with it, and are given the "Registered" role
(plus "Speaker" for panelists). Current members are grandfathered.

Library use (from discord_bot_loop.py, every ~120 s):
    import discord_gate
    discord_gate.gate_cycle(ctx)        # ctx needs .tok and .dry

CLI (all writes are skipped under --dry-run):
    --grandfather            give Registered to every current non-bot member
    --setup                  create role/#verify, lock the server down
    --restore FILE           put channel overwrites back from a backup
    --once                   run one gate cycle
    --force                  let --setup lock down without grandfathering

Rollout order: --grandfather, then --setup. Email fingerprints come from
scripts/gate_hashes.json (HMAC, pepper in env GATE_PEPPER). Neither emails nor
codes are ever logged or stored in plain form. Python 3.9 compatible.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import discord_bot_loop as dbl                       # noqa: E402
from discord_bot_loop import DiscordError            # noqa: E402

log = logging.getLogger("botloop.gate")

LOUIS = "lhyman6@jh.edu"
STATE_PATH = os.path.join(HERE, "discord_gate_state.json")
HASHES_PATH = os.path.join(HERE, "gate_hashes.json")
LOGS = os.path.join(HERE, "logs")

REG_ROLE = "Registered"
ALLOW_ROLES = ["Registered", "Organiser", "Speaker", "Volunteer"]
VERIFY_NAME = "verify"
WELCOME_NAME = "welcome"
VERIFY_TOPIC = ("New here? Check your DMs from the conference bot "
                "to confirm you registered.")

VIEW = 1 << 10
SEND = 1 << 11
HISTORY = 1 << 16

CODE_TTL_S = 30 * 60
MAX_ATTEMPTS = 5
MAX_CODES_PER_DAY = 3
MAX_NOMATCH = 5
MAX_CHATTER = 5
RETRY_DM_S = 600
CACHE_S = 600

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
CODE_RE = re.compile(r"^\s*(\d{3})\s?(\d{3})\s*$")

WELCOME_DM = ("Welcome to the AI and History Conference 2026 server. To unlock "
              "the channels I need to confirm that you registered. Reply here "
              "with the email address you registered with.")
CODE_SENT_DM = "I've emailed a code to that address. Reply with it here."
NOMATCH_DM = ("I couldn't find that address. Try the email you used to "
              "register, or reply HELP.")
OK_DM = "You're in. Welcome."
REMIND_DM = ("Reply here with the email address you registered with, or reply "
             "HELP and an organiser will look at it.")
DMS_CLOSED = ("Please allow direct messages from server members (Server name "
              "→ Privacy Settings) so I can verify you.")
NO_EMAIL_IN_VERIFY = ("I deleted your message in #verify because it contained "
                      "an email address, which others could read. Please reply "
                      "to my direct message with it instead.")
PIN_TEXT = (
    "This server is for registered attendees and speakers. To get in, check "
    "your direct messages from the conference bot and reply with the email "
    "address you registered with. I will email you a short code to confirm it "
    "is you.\n\nIf you did not get a DM, allow direct messages from server "
    "members (Server name → Privacy Settings). If you have not registered yet, "
    "do so at https://proflouishyman.github.io/ai_conference_2026/register.html "
    "and then come back.\n\nPlease do not post your email address in this "
    "channel. Any message containing one is deleted.")


# ------------------------------------------------------------------ helpers
def fp(pepper: str, email: str) -> str:
    return hmac.new(pepper.encode(), email.strip().lower().encode(),
                    hashlib.sha256).hexdigest()


def code_hash(pepper: str, uid: str, code: str) -> str:
    return hmac.new(pepper.encode(), ("code:%s:%s" % (uid, code)).encode(),
                    hashlib.sha256).hexdigest()


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def is_dm_closed(exc) -> bool:
    return "50007" in str(exc)


class RealApi:
    def __init__(self, tok, dry):
        self.tok, self.dry = tok, dry

    def call(self, method, path, payload=None):
        if self.dry and method != "GET":
            log.info("DRY-RUN %s %s", method, path.split("?")[0])
            return {"id": "dry"}
        return dbl.dcall(method, path, self.tok, payload)


def real_mail(to: str, subject: str, body: str, dry: bool) -> bool:
    """Reuse discord_bot_loop.send_email (single fixed recipient global)."""
    return dbl.send_email(subject, body, dry, to=to)


def guild_id(api) -> str:
    return dbl.GUILD or api.call("GET", "/users/@me/guilds")[0]["id"]


def fetch_members(api, gid):
    out, after = [], "0"
    while True:
        page = api.call("GET", "/guilds/%s/members?limit=1000&after=%s" % (gid, after))
        out.extend(page)
        if len(page) < 1000:
            return out
        after = max(page, key=lambda m: int(m["user"]["id"]))["user"]["id"]


def role_map(api, gid):
    return {r["name"]: r["id"] for r in api.call("GET", "/guilds/%s/roles" % gid)}


# ------------------------------------------------------------------ gate
class Gate:
    def __init__(self, api, mail, gid, me_id, pepper, hashes, state_path=STATE_PATH,
                 now=time.time, dry=False):
        self.api, self.mail, self.gid, self.me_id = api, mail, gid, me_id
        self.pepper, self.hashes, self.now, self.dry = pepper, hashes, now, dry
        self.state_path = state_path
        self.st = load_json(state_path, {})
        for k, v in (("grandfather_done", None), ("known", []), ("pending", {}),
                     ("verify_last", None), ("verified", {})):
            self.st.setdefault(k, v)
        self.roles, self.verify_id, self.cache_at = {}, None, 0.0
        self.activity = 0

    # -- persistence / plumbing
    def save(self):
        if not self.dry:
            save_json(self.state_path, self.st)

    def refresh(self):
        if self.now() - self.cache_at < CACHE_S and self.roles:
            return
        self.roles = role_map(self.api, self.gid)
        chans = self.api.call("GET", "/guilds/%s/channels" % self.gid)
        self.verify_id = next((c["id"] for c in chans
                               if c["type"] == 0 and c["name"] == VERIFY_NAME), None)
        self.cache_at = self.now()

    def dm(self, p, text):
        """Send a DM on the user's channel. Returns message id."""
        m = self.api.call("POST", "/channels/%s/messages" % p["dm"], {"content": text})
        self.activity += 1
        return m.get("id")

    def louis(self, subject, lines):
        self.mail(LOUIS, subject, "\n".join(lines), self.dry)

    # -- main entry
    def cycle(self) -> int:
        if not self.st["grandfather_done"]:
            log.info("gate inactive: --grandfather has not been completed")
            return 0
        if not self.pepper or not self.hashes:
            log.error("gate inactive: GATE_PEPPER or gate_hashes.json missing")
            return 0
        self.refresh()
        members = fetch_members(self.api, self.gid)
        by_id = {m["user"]["id"]: m for m in members}
        reg = self.roles.get(REG_ROLE)
        known = set(self.st["known"])
        for uid, m in by_id.items():
            if uid in known or m["user"].get("bot") or uid == self.me_id:
                continue
            known.add(uid)
            roles = m.get("roles", [])
            if reg in roles or self.roles.get("Organiser") in roles:
                continue
            if uid not in self.st["pending"]:
                self.st["pending"][uid] = {
                    "user": m["user"].get("username", "?"), "state": "new",
                    "attempts": 0, "sends": [], "nomatch": 0, "chatter": 0,
                    "joined": self.now()}
                log.info("gate: new member %s", uid)
        # forget people who left: if they rejoin, Discord has stripped their
        # roles, so they must be treated as new and verified again
        self.st["known"] = sorted(known & set(by_id)) if by_id else sorted(known)
        for uid in list(self.st["pending"]):          # left, or got the role by hand
            m = by_id.get(uid)
            if m is None or reg in m.get("roles", []):
                self.st["pending"].pop(uid)
                log.info("gate: %s no longer pending", uid)
        self.save()
        for uid in list(self.st["pending"]):
            try:
                self.step_user(uid)
            except DiscordError as exc:
                log.warning("gate: user %s: %s", uid, exc)
            except Exception as exc:                  # isolate one user's failure
                log.exception("gate: user %s crashed: %s", uid, type(exc).__name__)
        try:
            self.poll_verify()
        except DiscordError as exc:
            log.warning("gate: #verify poll: %s", exc)
        self.save()
        return self.activity

    # -- per user
    def step_user(self, uid):
        p = self.st["pending"][uid]
        if p["state"] == "new" or (p["state"] == "dm_closed"
                                   and self.now() >= p.get("retry_at", 0)):
            self.open_dm(uid, p)
        if p["state"] in ("need_email", "need_code"):
            self.poll_dm(uid, p)

    def open_dm(self, uid, p):
        try:
            ch = self.api.call("POST", "/users/@me/channels", {"recipient_id": uid})
            p["dm"] = ch["id"]
            p["last"] = self.dm(p, WELCOME_DM)
        except DiscordError as exc:
            if not is_dm_closed(exc):
                raise
            p["state"] = "dm_closed"
            p["retry_at"] = self.now() + RETRY_DM_S
            if not p.get("verify_notified") and self.verify_id:
                self.api.call("POST", "/channels/%s/messages" % self.verify_id, {
                    "content": "<@%s> %s" % (uid, DMS_CLOSED),
                    "allowed_mentions": {"users": [uid], "parse": []}})
                p["verify_notified"] = True
                self.activity += 1
            log.info("gate: DMs closed for %s", uid)
            self.save()
            return
        p["state"] = "need_email"
        log.info("gate: welcomed %s", uid)
        self.save()

    def poll_dm(self, uid, p):
        if p["dm"] == "dry":
            return
        q = "/channels/%s/messages?limit=50" % p["dm"]
        if p.get("last"):
            q += "&after=%s" % p["last"]
        msgs = sorted(self.api.call("GET", q), key=lambda m: int(m["id"]))
        for m in msgs:
            p["last"] = m["id"]
            if m.get("author", {}).get("id") != uid or m.get("author", {}).get("bot"):
                continue
            self.handle_text(uid, p, m.get("content", ""))
            self.save()
            if p["state"] in ("done", "locked"):
                break

    def handle_text(self, uid, p, text):
        if p["state"] == "locked":
            return
        t = text.strip()
        if t.lower() in ("help", "help!", "/help"):
            self.louis("Discord verification needs you",
                       ["%s (id %s) replied HELP to the verification bot." %
                        (p["user"], uid)])
            self.dm(p, "I've asked an organiser to look at it. You will hear "
                       "from us by email or here.")
            log.info("gate: %s asked for help", uid)
            return
        cm = CODE_RE.match(t)
        if cm:
            return self.check_code(uid, p, cm.group(1) + cm.group(2))
        em = EMAIL_RE.search(t)
        if em:
            return self.check_email(uid, p, em.group(0))
        p["chatter"] += 1
        if p["chatter"] <= MAX_CHATTER:
            self.dm(p, REMIND_DM)

    def check_email(self, uid, p, email):
        now = self.now()
        h = fp(self.pepper, email)
        hit = self.hashes.get(h)
        if hit is None:
            p["nomatch"] += 1
            log.info("gate: no match for %s (%d)", uid, p["nomatch"])
            if p["nomatch"] > MAX_NOMATCH:
                return                                  # stop replying to a guesser
            self.dm(p, NOMATCH_DM)
            self.louis("Discord verification needs you", [
                "%s (id %s) tried an address that is not in the registration "
                "list." % (p["user"], uid), "Address they tried: %s" % email])
            return
        p["sends"] = [t for t in p["sends"] if now - t < 86400]
        if len(p["sends"]) >= MAX_CODES_PER_DAY:
            self.dm(p, "I've already emailed you three codes today. Please try "
                       "again tomorrow, or reply HELP.")
            log.info("gate: %s hit the daily code limit", uid)
            return
        code = "%06d" % secrets.randbelow(10 ** 6)
        body = ("Your code is %s.\n\nIt is valid for 30 minutes. Reply with it "
                "to the conference bot in your Discord direct messages.\n\nIf "
                "you did not ask for this, you can ignore this email.\n\n"
                "The AI and History Conference organisers" % code)
        ok = self.mail(email.strip().lower(), "Your AI and History Conference Discord code",
                       body, self.dry)
        if not ok:
            self.dm(p, "I couldn't send that email just now. An organiser has "
                       "been told and will help.")
            self.louis("Discord verification needs you", [
                "Code email failed for %s (id %s)." % (p["user"], uid)])
            return
        p["sends"].append(now)
        p["code_hash"] = code_hash(self.pepper, uid, code)
        p["code_exp"] = now + CODE_TTL_S
        p["attempts"] = 0
        p["speaker"] = bool(hit.get("speaker"))
        p["state"] = "need_code"
        self.dm(p, CODE_SENT_DM)
        log.info("gate: code sent for %s", uid)

    def check_code(self, uid, p, code):
        if p["state"] != "need_code":
            self.dm(p, "Please send me the email address you registered with first.")
            return
        if self.now() > p.get("code_exp", 0):
            p["state"] = "need_email"
            p.pop("code_hash", None)
            self.dm(p, "That code has expired. Reply with your registered "
                       "email address and I'll send a new one.")
            return
        if hmac.compare_digest(p["code_hash"], code_hash(self.pepper, uid, code)):
            return self.admit(uid, p)
        p["attempts"] += 1
        left = MAX_ATTEMPTS - p["attempts"]
        log.info("gate: wrong code from %s (%d)", uid, p["attempts"])
        if left > 0:
            self.dm(p, "That code isn't right. You have %d %s left." %
                    (left, "try" if left == 1 else "tries"))
            return
        p["state"] = "locked"
        p.pop("code_hash", None)
        self.dm(p, "Too many wrong codes. An organiser will follow up with you.")
        self.louis("Discord verification needs you", [
            "%s (id %s) was locked out after %d wrong codes." %
            (p["user"], uid, MAX_ATTEMPTS)])

    def admit(self, uid, p):
        give = [REG_ROLE] + (["Speaker"] if p.get("speaker") else [])
        for name in give:
            rid = self.roles.get(name)
            if not rid:
                raise DiscordError("config", "role %s missing" % name)
            self.api.call("PUT", "/guilds/%s/members/%s/roles/%s" %
                          (self.gid, uid, rid))
        self.st["verified"][uid] = self.now()
        p["state"] = "done"
        self.dm(p, OK_DM)
        self.st["pending"].pop(uid, None)
        log.info("gate: admitted %s%s", uid, " as speaker" if p.get("speaker") else "")
        self.save()

    # -- #verify hygiene
    def poll_verify(self):
        if not self.verify_id:
            return
        last = self.st["verify_last"]
        if not last:                                    # first look: skip history
            latest = self.api.call("GET", "/channels/%s/messages?limit=1" % self.verify_id)
            self.st["verify_last"] = latest[0]["id"] if latest else "0"
            return
        msgs = sorted(self.api.call(
            "GET", "/channels/%s/messages?limit=50&after=%s" % (self.verify_id, last)),
            key=lambda m: int(m["id"]))
        for m in msgs:
            self.st["verify_last"] = m["id"]
            a = m.get("author", {})
            if a.get("bot") or a.get("id") == self.me_id or m.get("webhook_id"):
                continue
            if not EMAIL_RE.search(m.get("content", "")):
                continue
            self.api.call("DELETE", "/channels/%s/messages/%s" % (self.verify_id, m["id"]))
            log.info("gate: deleted email post in #verify by %s", a.get("id"))
            self.activity += 1
            try:
                ch = self.api.call("POST", "/users/@me/channels", {"recipient_id": a["id"]})
                self.api.call("POST", "/channels/%s/messages" % ch["id"],
                              {"content": NO_EMAIL_IN_VERIFY})
            except DiscordError as exc:
                log.info("gate: could not DM %s about #verify (%s)", a.get("id"),
                         "closed" if is_dm_closed(exc) else "error")
            self.save()


# ------------------------------------------------------------------ loop API
def gate_cycle(ctx) -> int:
    """Call from the bot loop every ~120 s. Never raises. Returns activity count."""
    try:
        g = getattr(ctx, "_gate", None)
        if g is None:
            dbl.load_env()
            api = RealApi(ctx.tok, ctx.dry)
            gid = guild_id(api)
            dbl.GUILD = gid
            g = ctx._gate = Gate(api, real_mail, gid, ctx.me_id,
                                 os.environ.get("GATE_PEPPER", ""), {}, dry=ctx.dry)
        mt = os.path.getmtime(HASHES_PATH) if os.path.exists(HASHES_PATH) else None
        if mt != getattr(g, "_hash_mtime", 0):
            g.hashes = load_json(HASHES_PATH, {})
            g._hash_mtime = mt
        g.activity = 0
        return g.cycle()
    except Exception as exc:
        log.exception("gate_cycle failed: %s", type(exc).__name__)
        return 0


# ------------------------------------------------------------------ setup
def desired_overwrites(ch, gid, role_ids):
    """Return (new_overwrites, note) or (None, reason) if nothing to change."""
    ows = {o["id"]: {"id": o["id"], "type": o["type"],
                     "allow": int(o["allow"]), "deny": int(o["deny"])}
           for o in ch.get("permission_overwrites", [])}
    ev = ows.setdefault(gid, {"id": gid, "type": 0, "allow": 0, "deny": 0})
    before = {k: dict(v) for k, v in ows.items()}
    name = ch["name"]
    if name in (WELCOME_NAME, VERIFY_NAME) and ch["type"] == 0:
        ev["allow"] |= VIEW | HISTORY
        ev["deny"] &= ~(VIEW | HISTORY)
        if name == WELCOME_NAME:
            ev["deny"] |= SEND
            ev["allow"] &= ~SEND
            note = "public, read-only"
        else:
            ev["allow"] |= SEND
            ev["deny"] &= ~SEND
            note = "public, anyone can post"
    elif ev["deny"] & VIEW:
        return None, "already hidden from @everyone (left untouched)"
    else:
        ev["deny"] |= VIEW
        ev["allow"] &= ~VIEW
        for rid in role_ids:
            r = ows.setdefault(rid, {"id": rid, "type": 0, "allow": 0, "deny": 0})
            r["allow"] |= VIEW
            r["deny"] &= ~VIEW
        note = "hidden from @everyone, visible to Registered/Organiser/Speaker/Volunteer"
    if ows == before:
        return None, "already correct"
    for o in ows.values():
        o["allow"], o["deny"] = str(o["allow"]), str(o["deny"])
    return list(ows.values()), note


def members_missing_registered(api, gid, reg_id):
    return [m for m in fetch_members(api, gid)
            if not m["user"].get("bot") and (not reg_id or reg_id not in m.get("roles", []))]


def cmd_grandfather(api, gid, dry):
    roles = role_map(api, gid)
    reg = roles.get(REG_ROLE)
    if not reg:
        print("Role 'Registered' missing: %s" % ("would create" if dry else "creating"))
        if not dry:
            reg = api.call("POST", "/guilds/%s/roles" % gid,
                           {"name": REG_ROLE, "permissions": "0", "mentionable": False})["id"]
    members = fetch_members(api, gid)
    todo = [m for m in members if not m["user"].get("bot")
            and (not reg or reg not in m.get("roles", []))]
    print("%d non-bot members, %d %s Registered" %
          (len([m for m in members if not m["user"].get("bot")]), len(todo),
           "would be given" if dry else "given"))
    if dry:
        return 0
    for m in todo:
        api.call("PUT", "/guilds/%s/members/%s/roles/%s" % (gid, m["user"]["id"], reg))
    st = load_json(STATE_PATH, {})
    st.setdefault("pending", {})
    st.setdefault("verified", {})
    st["known"] = sorted(m["user"]["id"] for m in members)
    st["grandfathered"] = sorted(m["user"]["id"] for m in todo)
    st["grandfather_done"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_PATH, st)
    print("Grandfathering complete; state saved.")
    return 0


def cmd_setup(api, gid, dry, force):
    roles = role_map(api, gid)
    chans = api.call("GET", "/guilds/%s/channels" % gid)
    reg = roles.get(REG_ROLE)
    print("PLAN (%s)" % ("dry-run" if dry else "APPLY"))
    # guard
    st = load_json(STATE_PATH, {})
    missing = members_missing_registered(api, gid, reg)
    guard_ok = bool(st.get("grandfather_done")) and not missing
    if not guard_ok:
        why = ("grandfathering not recorded" if not st.get("grandfather_done")
               else "%d members still lack Registered" % len(missing))
        if force:
            print("GUARD: %s, continuing because --force" % why)
        elif dry:
            print("GUARD: a real --setup would REFUSE (%s). Run --grandfather first." % why)
        else:
            print("REFUSING lockdown: %s. Run --grandfather first, or use --force." % why)
            return 2
    else:
        print("GUARD: ok, all members hold Registered")
    print("- role Registered: %s" % ("exists" if reg else "CREATE"))
    welcome = next((c for c in chans if c["type"] == 0 and c["name"] == WELCOME_NAME), None)
    verify = next((c for c in chans if c["type"] == 0 and c["name"] == VERIFY_NAME), None)
    if not welcome:
        print("ABORT: no #welcome channel found")
        return 1
    print("- channel #verify: %s (category %s)" % (
        "exists" if verify else "CREATE", welcome.get("parent_id")))
    allow_names = ALLOW_ROLES
    ph = {n: roles.get(n, "<new %s>" % n) for n in allow_names}
    missing_roles = [n for n in allow_names if n not in roles and n != REG_ROLE]
    if missing_roles:
        print("ABORT: roles missing: %s" % missing_roles)
        return 1
    cat_names = {c["id"]: c["name"] for c in chans if c["type"] == 4}
    plan = []
    for c in sorted(chans, key=lambda c: (c["type"] != 4, c.get("position", 0))):
        new, note = desired_overwrites(c, gid, list(ph.values()))
        plan.append((c, new, note))
        print("  %-22s %-10s %s" % (c["name"][:22], "category" if c["type"] == 4
              else cat_names.get(c.get("parent_id"), "-")[:10],
              note if new is None else "CHANGE: " + note))
    n_change = sum(1 for _, n, _ in plan if n is not None)
    print("%d channels/categories to change, %d left alone" % (n_change, len(plan) - n_change))
    if dry:
        return 0
    os.makedirs(LOGS, exist_ok=True)
    bk = os.path.join(LOGS, "overwrites_backup_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    save_json(bk, {"guild": gid, "channels": [
        {"id": c["id"], "name": c["name"], "type": c["type"],
         "permission_overwrites": c.get("permission_overwrites", [])} for c in chans]})
    print("Backup written: %s" % bk)
    if not reg:
        reg = api.call("POST", "/guilds/%s/roles" % gid,
                       {"name": REG_ROLE, "permissions": "0", "mentionable": False})["id"]
        roles[REG_ROLE] = reg
    role_ids = [roles[n] for n in allow_names]
    if not verify:
        verify = api.call("POST", "/guilds/%s/channels" % gid, {
            "name": VERIFY_NAME, "type": 0, "topic": VERIFY_TOPIC,
            "parent_id": welcome.get("parent_id"),
            "permission_overwrites": [{"id": gid, "type": 0, "allow": str(VIEW | SEND | HISTORY), "deny": "0"}]})
        print("Created #verify")
    elif verify.get("topic") != VERIFY_TOPIC:
        api.call("PATCH", "/channels/%s" % verify["id"], {"topic": VERIFY_TOPIC})
    pinned = []
    try:
        pinned = [m for m in api.call("GET", "/channels/%s/pins" % verify["id"])
                  if m.get("author", {}).get("bot")]
    except DiscordError:
        pass
    if not pinned:
        m = api.call("POST", "/channels/%s/messages" % verify["id"], {"content": PIN_TEXT})
        api.call("PUT", "/channels/%s/pins/%s" % (verify["id"], m["id"]))
        print("Posted and pinned the instructions")
    # roles' VIEW grants go in the same PATCH as the @everyone deny, per channel
    for c, _, _ in plan:
        new, _ = desired_overwrites(c, gid, role_ids)
        if new is not None:
            api.call("PATCH", "/channels/%s" % c["id"], {"permission_overwrites": new})
    # make sure #verify itself is correct if it already existed
    v = api.call("GET", "/channels/%s" % verify["id"])
    new, _ = desired_overwrites(v, gid, role_ids)
    if new is not None:
        api.call("PATCH", "/channels/%s" % verify["id"], {"permission_overwrites": new})
    print("Lockdown applied. Undo with: --restore %s" % bk)
    return 0


def cmd_restore(api, gid, path, dry):
    bk = load_json(path, None)
    if not bk or bk.get("guild") != gid:
        print("Backup unreadable or for another guild")
        return 1
    for c in bk["channels"]:
        print("%s #%s: %d overwrites" % ("would restore" if dry else "restoring",
                                          c["name"], len(c["permission_overwrites"])))
        if not dry:
            api.call("PATCH", "/channels/%s" % c["id"],
                     {"permission_overwrites": c["permission_overwrites"]})
    print("Note: the Registered role and #verify are left in place.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--grandfather", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dbl.load_env()
    tok = dbl.token()
    api = RealApi(tok, a.dry_run)
    gid = guild_id(api)
    dbl.GUILD = gid
    if a.grandfather:
        return cmd_grandfather(api, gid, a.dry_run)
    if a.setup:
        return cmd_setup(api, gid, a.dry_run, a.force)
    if a.restore:
        return cmd_restore(api, gid, a.restore, a.dry_run)
    if a.once:
        class C:
            pass
        c = C()
        c.tok, c.dry, c.me_id = tok, a.dry_run, api.call("GET", "/users/@me")["id"]
        print("activity:", gate_cycle(c))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
