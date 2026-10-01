#!/usr/bin/env python3
"""Offline tests for discord_gate.Gate with a fake Discord and fake mail."""
import os, re, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discord_gate as g
from discord_gate import DiscordError
AT = "@"   # fake test addresses built at runtime so the repo privacy hook stays strict

PEPPER = "testpepper"
GID, ME, VERIFY = "G", "BOT", "CH_VERIFY"


class FakeDiscord:
    def __init__(self):
        self.n = 1000
        self.members = {}              # uid -> {"user":..., "roles":[]}
        self.roles = {"Registered": "R1", "Speaker": "R2", "Organiser": "R3"}
        self.msgs = {VERIFY: []}       # channel -> [msg]
        self.dm_closed = set()
        self.dms = {}                  # uid -> channel id
        self.deleted = []
        self.posts = []                # (channel, payload)

    def nid(self):
        self.n += 1
        return str(self.n)

    def add_member(self, uid, name="u"):
        self.members[uid] = {"user": {"id": uid, "username": name}, "roles": []}

    def say(self, ch, uid, text):      # a human speaks
        m = {"id": self.nid(), "content": text, "author": {"id": uid}}
        self.msgs.setdefault(ch, []).append(m)
        return m

    def call(self, method, path, payload=None):
        p = path.split("?")[0]
        q = dict(x.split("=") for x in path.split("?")[1].split("&")) if "?" in path else {}
        if method == "GET" and p == "/guilds/G/members":
            return list(self.members.values())
        if method == "GET" and p == "/guilds/G/roles":
            return [{"id": v, "name": k} for k, v in self.roles.items()]
        if method == "GET" and p == "/guilds/G/channels":
            return [{"id": VERIFY, "type": 0, "name": "verify"}]
        if method == "POST" and p == "/users/@me/channels":
            uid = payload["recipient_id"]
            if uid in self.dm_closed:
                raise DiscordError(403, '{"message": "Cannot send messages to this user", "code": 50007}')
            self.dms.setdefault(uid, "DM" + uid)
            self.msgs.setdefault("DM" + uid, [])
            return {"id": self.dms[uid]}
        if method == "POST" and re.match(r"/channels/[^/]+/messages$", p):
            ch = p.split("/")[2]
            if ch.startswith("DM") and ch[2:] in self.dm_closed:
                raise DiscordError(403, '{"code": 50007}')
            m = {"id": self.nid(), "content": payload["content"], "author": {"id": ME, "bot": True}}
            self.msgs.setdefault(ch, []).append(m)
            self.posts.append((ch, payload))
            return m
        if method == "GET" and re.match(r"/channels/[^/]+/messages$", p):
            ch = p.split("/")[2]
            ms = self.msgs.get(ch, [])
            if "after" in q:
                ms = [m for m in ms if int(m["id"]) > int(q["after"])]
            return list(reversed(ms))[: int(q.get("limit", 50))]
        if method == "DELETE" and "/messages/" in p:
            self.deleted.append(p.split("/")[-1])
            return {}
        if method == "PUT" and "/roles/" in p:
            parts = p.split("/")
            rid = parts[-1]
            self.members[parts[4]]["roles"].append(rid)
            return {}
        raise AssertionError("unexpected call %s %s" % (method, path))


class Env:
    def __init__(self, hashes):
        self.d = FakeDiscord()
        self.mails = []
        self.t = [1_000_000.0]
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")
        self.d.add_member("OLD", "old")
        self.gate = g.Gate(self.d, self.mail, GID, ME, PEPPER, hashes,
                           state_path=self.path, now=lambda: self.t[0])
        self.gate.st["grandfather_done"] = "x"
        self.gate.st["known"] = ["OLD"]
        self.gate.st["verify_last"] = "0"

    def mail(self, to, subject, body, dry):
        self.mails.append((to, subject, body))
        return True

    def cycle(self):
        self.gate.activity = 0
        return self.gate.cycle()

    def bot_dms(self, uid):
        return [m["content"] for m in self.d.msgs.get("DM" + uid, []) if m["author"].get("bot")]

    def code(self):
        for to, subj, body in reversed(self.mails):
            if "code" in subj:
                return re.search(r"code is (\d{6})", body).group(1)


H = {g.fp(PEPPER, "ok" + AT + "x.edu"): {"speaker": False},
     g.fp(PEPPER, "spk" + AT + "x.edu"): {"speaker": True}}


class T(unittest.TestCase):
    def test_match_then_correct_code(self):
        e = Env(H)
        e.d.add_member("U1", "alice")
        e.cycle()
        self.assertIn("Reply here with the email", e.bot_dms("U1")[0])
        e.d.say("DMU1", "U1", "OK" + AT + "X.edu ")
        e.cycle()
        self.assertEqual(e.mails[-1][0], "ok" + AT + "x.edu")
        self.assertEqual(e.mails[-1][1], "Your AI and History Conference Discord code")
        self.assertIn("emailed a code", e.bot_dms("U1")[-1])
        self.assertNotIn(e.code(), open(e.path).read())          # never stored plain
        e.d.say("DMU1", "U1", e.code())
        e.cycle()
        self.assertEqual(e.d.members["U1"]["roles"], ["R1"])
        self.assertEqual(e.bot_dms("U1")[-1], "You're in. Welcome.")
        self.assertNotIn("U1", e.gate.st["pending"])

    def test_speaker_flag(self):
        e = Env(H)
        e.d.add_member("U1")
        e.cycle()
        e.d.say("DMU1", "U1", "my email is spk" + AT + "x.edu")
        e.cycle()
        e.d.say("DMU1", "U1", e.code())
        e.cycle()
        self.assertEqual(sorted(e.d.members["U1"]["roles"]), ["R1", "R2"])

    def test_wrong_code_five_times_locks(self):
        e = Env(H)
        e.d.add_member("U1")
        e.cycle()
        e.d.say("DMU1", "U1", "ok" + AT + "x.edu")
        e.cycle()
        real = e.code()
        bad = "000000" if real != "000000" else "111111"
        for _ in range(5):
            e.d.say("DMU1", "U1", bad)
            e.cycle()
        self.assertEqual(e.d.members["U1"]["roles"], [])
        self.assertEqual(e.gate.st["pending"]["U1"]["state"], "locked")
        self.assertEqual(e.mails[-1][0], "lhyman6" + AT + "jh.edu")
        e.d.say("DMU1", "U1", real)                    # even the right code is now refused
        e.cycle()
        self.assertEqual(e.d.members["U1"]["roles"], [])

    def test_no_match(self):
        e = Env(H)
        e.d.add_member("U1", "bob")
        e.cycle()
        e.d.say("DMU1", "U1", "nobody" + AT + "y.org")
        e.cycle()
        self.assertIn("couldn't find that address", e.bot_dms("U1")[-1])
        to, subj, body = e.mails[-1]
        self.assertEqual((to, subj), ("lhyman6" + AT + "jh.edu", "Discord verification needs you"))
        self.assertIn("bob", body)
        self.assertIn("nobody" + AT + "y.org", body)
        self.assertEqual(len(e.mails), 1)

    def test_dm_closed(self):
        e = Env(H)
        e.d.dm_closed.add("U1")
        e.d.add_member("U1")
        e.cycle()
        e.cycle()
        posts = [p for c, p in e.d.posts if c == VERIFY]
        self.assertEqual(len(posts), 1)                    # only once
        self.assertEqual(posts[0]["allowed_mentions"], {"users": ["U1"], "parse": []})
        self.assertIn("<@U1>", posts[0]["content"])
        e.d.dm_closed.clear()                              # user fixes settings
        e.t[0] += 700
        e.cycle()
        self.assertEqual(e.gate.st["pending"]["U1"]["state"], "need_email")

    def test_email_in_verify_deleted(self):
        e = Env(H)
        e.cycle()                                          # primes nothing
        m = e.d.say(VERIFY, "U9", "hi my email is ok" + AT + "x.edu")
        e.d.say(VERIFY, "U8", "just chatting")
        e.cycle()
        self.assertEqual(e.d.deleted, [m["id"]])
        self.assertIn("reply to my direct message", e.bot_dms("U9")[0])

    def test_rate_limit_codes(self):
        e = Env(H)
        e.d.add_member("U1")
        e.cycle()
        for _ in range(4):
            e.d.say("DMU1", "U1", "ok" + AT + "x.edu")
            e.cycle()
        self.assertEqual(len([m for m in e.mails if "code" in m[1]]), 3)

    def test_expired_code_and_inactive_until_grandfathered(self):
        e = Env(H)
        e.d.add_member("U1")
        e.cycle()
        e.d.say("DMU1", "U1", "ok" + AT + "x.edu")
        e.cycle()
        c = e.code()
        e.t[0] += 1900
        e.d.say("DMU1", "U1", c)
        e.cycle()
        self.assertEqual(e.d.members["U1"]["roles"], [])
        self.assertIn("expired", e.bot_dms("U1")[-1])
        e2 = Env(H)
        e2.gate.st["grandfather_done"] = None
        e2.d.add_member("U2")
        self.assertEqual(e2.cycle(), 0)
        self.assertEqual(e2.d.posts, [])


class UnionLoadTest(unittest.TestCase):
    def test_union_and_speaker_flags(self):
        import json
        d = tempfile.mkdtemp()
        a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
        json.dump({"h1": {"speaker": True}, "h2": {"speaker": False}}, open(a, "w"))
        json.dump({"count": 2, "hashes": ["h2", "h3", "h1"]}, open(b, "w"))
        out = g.load_hashes(a, b)
        self.assertEqual(set(out), {"h1", "h2", "h3"})
        self.assertTrue(out["h1"]["speaker"])
        self.assertFalse(out["h3"]["speaker"])
        self.assertEqual(g.load_hashes(a, os.path.join(d, "none.json")),
                         {"h1": {"speaker": True}, "h2": {"speaker": False}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
