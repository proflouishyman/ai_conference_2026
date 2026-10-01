#!/usr/bin/env python3
"""Offline tests: bot-added glossary terms, A-Z master list edits, reject.
Fake Discord, temp copies of the data files. Nothing is posted anywhere."""
import json, os, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glossary_sync as gs
import publish_glossary as pg
import discord_bot_loop as loop

HERE = os.path.dirname(os.path.abspath(__file__))
ME, CH = "BOT", "JARGON"
DEF = ("Splitting text into small units, called tokens, that a language model "
       "reads. A token is often a word or a piece of one, so a long word can "
       "become several tokens.")


class FakeDiscord:
    def __init__(self, gloss):
        self.n = 1000
        self.msgs = []
        self.log = []
        for content in [pg.INTRO] + pg.chunk(pg.render(gloss)) + [pg.OUTRO]:
            self.add(content)

    def add(self, content, author=ME):
        self.n += 1
        self.msgs.append({"id": str(self.n), "content": content,
                          "author": {"id": author}})
        return self.msgs[-1]

    def call(self, method, path, payload=None):
        self.log.append((method, path))
        if method == "GET":
            return list(reversed(self.msgs))
        if method == "PATCH":
            mid = path.rsplit("/", 1)[1]
            m = next((m for m in self.msgs if m["id"] == mid), None)
            if m:                      # replies in other channels are not modelled
                m["content"] = payload["content"]
            return {}
        if method == "POST":
            return self.add(payload["content"])
        if method == "DELETE":
            mid = path.rsplit("/", 1)[1]
            self.msgs = [m for m in self.msgs if m["id"] != mid]
            return {}

    def run(self):
        return [m["content"] for m in self.msgs]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.gl = os.path.join(self.d, "glossary.json")
        g = json.load(open(os.path.join(HERE, "glossary.json")))
        g.pop("token", None)    # live glossary already has tokenization as an alias
        json.dump(g, open(self.gl, "w"), indent=1, sort_keys=True)
        self.fd = FakeDiscord(json.load(open(self.gl)))

    def tearDown(self):
        shutil.rmtree(self.d)


class SyncTest(Base):
    def test_add_clean_and_no_overwrite(self):
        reply = "**Tokenization**: " + DEF + gs_footer()
        k, e = gs.add_bot_term("Tokenization", reply, "https://discord.com/channels/1/2/3",
                               "tokenization", path=self.gl, today="2026-10-01")
        self.assertEqual(k, "tokenization")
        self.assertEqual(e["definition"], DEF)
        self.assertEqual(set(e), {"term", "definition", "source", "added",
                                  "question_permalink", "reviewed"})
        self.assertFalse(e["reviewed"])
        # existing human entry or alias is never overwritten
        before = json.load(open(self.gl))["abstraction"]
        k, why = gs.add_bot_term("Abstraction", DEF, "x", path=self.gl)
        self.assertIsNone(k)
        k, why = gs.add_bot_term("Agent", DEF, "x", path=self.gl)   # alias of agentic
        self.assertIsNone(k)
        self.assertEqual(json.load(open(self.gl))["abstraction"], before)
        self.assertFalse(os.path.exists(self.gl + ".tmp"))

    def test_rejects_address_like_text(self):
        k, why = gs.add_bot_term("Foo", DEF + " Mail a" + "@" + "b.org now.", "x", path=self.gl)
        self.assertIsNone(k)

    def test_master_edit_in_place_and_reflow(self):
        gloss = json.load(open(self.gl))
        gs.add_bot_term("Tokenization", DEF, "p", path=self.gl)
        gloss = json.load(open(self.gl))
        n_before, ids_before = len(self.fd.msgs), [m["id"] for m in self.fd.msgs]
        ops = gs.refresh_master(self.fd.call, CH, ME, gloss, out=lambda s: None)
        self.assertTrue(ops)
        self.assertEqual(len(self.fd.msgs), n_before)            # no overflow
        self.assertEqual([m["id"] for m in self.fd.msgs], ids_before)
        text = "\n".join(self.fd.run())
        self.assertIn("**Tokenization** *(AI-drafted)* — ", text)
        # right alphabetical place: after a T term, before a U/V/W term
        self.assertTrue(self.fd.msgs[0]["content"].startswith(pg.INTRO[:10]))
        self.assertTrue(self.fd.msgs[-1]["content"].startswith("**Missing a word?"))
        self.assertEqual([c for c in self.fd.run()[1:-1]],
                         pg.chunk(pg.render(gloss)))
        # idempotent
        self.assertEqual(gs.refresh_master(self.fd.call, CH, ME, gloss, out=lambda s: None), [])

    def test_overflow_appends_and_keeps_outro_last(self):
        gloss = json.load(open(self.gl))
        for i in range(30):
            gloss[f"zzterm{i}"] = {"term": f"Zzterm{i}", "definition": DEF * 2,
                                   "source": "bot", "reviewed": False}
        ids = [m["id"] for m in self.fd.msgs]
        gs.refresh_master(self.fd.call, CH, ME, gloss, out=lambda s: None)
        self.assertGreater(len(self.fd.msgs), len(ids))
        self.assertTrue(self.fd.msgs[-1]["content"].startswith("**Missing a word?"))
        self.assertEqual(self.fd.run()[1:-1], pg.chunk(pg.render(gloss)))
        self.assertTrue(all(len(c) <= 2000 for c in self.fd.run()))
        posts = [x for x in self.fd.log if x[0] == "POST"]
        self.assertGreaterEqual(len(posts), 1)

    def test_refuses_when_run_missing(self):
        self.fd.msgs = []
        with self.assertRaises(RuntimeError):
            gs.refresh_master(self.fd.call, CH, ME, {}, out=lambda s: None)

    def test_bot_replies_in_channel_are_not_mistaken_for_az(self):
        self.fd.add("**Tokenization**: " + DEF + gs_footer())     # bot reply after run
        gs.add_bot_term("Tokenization", DEF, "p", path=self.gl)
        gloss = json.load(open(self.gl))
        gs.refresh_master(self.fd.call, CH, ME, gloss, out=lambda s: None)
        self.assertTrue(self.fd.msgs[-1]["content"].startswith("**Tokenization**:"))

    def test_reviewed_drops_mark_and_reject_removes_only_bot(self):
        gs.add_bot_term("Tokenization", DEF, "p", path=self.gl)
        self.assertTrue(gs.mark_reviewed("Tokenization", path=self.gl))
        e = json.load(open(self.gl))["tokenization"]
        self.assertNotIn("AI-drafted", "".join(pg.render({"tokenization": e})))
        self.assertTrue(gs.remove_bot_term("Tokenization", path=self.gl))
        self.assertFalse(gs.remove_bot_term("Abstraction", path=self.gl))
        self.assertIn("abstraction", json.load(open(self.gl)))

    def test_rate_limit_batches(self):
        t = [0.0]
        r = gs.Refresher(60, clock=lambda: t[0])
        self.assertFalse(r.due())
        r.mark(); self.assertTrue(r.due()); r.done()
        t[0] = 10; r.mark(); self.assertFalse(r.due())
        t[0] = 61; self.assertTrue(r.due())


def gs_footer():
    return loop.FOOTER_AI


class LoopIntegration(Base):
    """handle() end to end with a fake Discord and a stubbed LLM."""
    def setUp(self):
        super().setUp()
        self.pend = os.path.join(self.d, "pending_review.json")
        self.saved = (loop.GLOSSARY, loop.PENDING, loop.STATE, loop.GUILD,
                      loop.dcall, loop.classify, loop.send_email, loop.time.sleep)
        loop.GLOSSARY, loop.PENDING, loop.GUILD = self.gl, self.pend, "G"
        loop.STATE = os.path.join(self.d, "state.json")
        loop.send_email = lambda *a, **k: True
        loop.time.sleep = lambda s: None
        self.llm_calls = 0

        def classify(q, title=""):
            self.llm_calls += 1
            return ({"is_question": True, "in_scope": True, "kind": "jargon",
                     "answer": DEF, "term": "Tokenization", "for_speaker": False,
                     "guard": ""}, "stub-model")
        loop.classify = classify

        def dcall(method, path, tok, payload=None):
            if path.startswith("/channels/Q/messages") and method == "POST":
                return {"id": "9001"}
            return self.fd.call(method, path, payload)
        loop.dcall = dcall
        self.ctx = loop.Ctx("tok", False, ME)
        self.ctx.chans = {CH: "jargon-for-historians", "Q": "ask-anything"}
        self.ctx.master = gs.Refresher(60, clock=lambda: 1e9)

    def tearDown(self):
        (loop.GLOSSARY, loop.PENDING, loop.STATE, loop.GUILD, loop.dcall,
         loop.classify, loop.send_email, loop.time.sleep) = self.saved
        super().tearDown()

    def ask(self, mid):
        m = {"id": mid, "content": "what is tokenization?", "timestamp": "2026-10-01T12:00:00+00:00",
             "author": {"id": "U1", "username": "someone_private"}}
        return loop.handle(self.ctx, m, "Q", {"llm": 0}, True)

    def test_full_flow(self):
        before = json.load(open(self.gl))
        self.assertEqual(self.ask("500"), "reply")
        self.assertEqual(self.llm_calls, 1)
        after = json.load(open(self.gl))
        added = {k: v for k, v in after.items() if k not in before}
        self.assertEqual(list(added), ["tokenization"])
        e = added["tokenization"]
        self.assertEqual(e["question_permalink"], "https://discord.com/channels/G/Q/500")
        self.assertNotIn("someone_private", json.dumps(after))
        self.assertNotIn("AI-generated", e["definition"])
        self.assertFalse(e["definition"].startswith("**"))
        # in-memory immediately, and master list patched (jargon-channel run)
        self.assertIn("tokenization", self.ctx.gloss)
        self.assertIn("**Tokenization** *(AI-drafted)*", "\n".join(self.fd.run()))
        pend = json.load(open(self.pend))
        self.assertFalse(pend["9001"]["reviewed"])
        # second identical question: glossary hit, no LLM
        self.assertEqual(self.ask("501"), "reply")
        self.assertEqual(self.llm_calls, 1)
        # reload from disk sees it too
        self.ctx.gloss = {}
        self.ctx.gloss_mtime = None
        self.ctx.reload_gloss()
        self.assertIn("tokenization", self.ctx.gloss)


if __name__ == "__main__":
    unittest.main(verbosity=2)
