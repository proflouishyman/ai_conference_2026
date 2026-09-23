#!/usr/bin/env python3
"""Audit outgoing mail for the two failures seen on 2026-09-22.

1. A reply meant for Henry Farrell went to Tenza Nazur, because thread 32194
   holds 37 unrelated correspondents under one subject line ("A quick question
   for local attendees"). Outlook groups by normalized subject, not by
   conversation, so replying to such a thread lands on whoever's message was
   picked rather than the intended person.

2. Nothing caught it afterwards: record-send had not been called since
   2026-09-15, so 38 sends had no send_log row to reconcile against.

Usage: audit_sends.py [SINCE_DATE]   (default 2026-09-15)
"""
import sqlite3, sys, json

DB = "/Users/louishyman/.openclaw/workspaces/polly-workspace/mail-index.db"
since = sys.argv[1] if len(sys.argv) > 1 else "2026-09-15"
c = sqlite3.connect(DB)

# Louis sends from four addresses. Read them from owner_addresses rather than
# matching one spelling -- the misdirected message came from lhyman6@jh.edu,
# and an audit keyed on "louishyman" missed the very send it existed to catch.
owners = [r[0].lower() for r in c.execute("SELECT address FROM owner_addresses")]
ph = ",".join("?" * len(owners))

sent = c.execute(f"""SELECT provider_id, datetime(received_at), to_text, subject, thread_id
    FROM messages
    WHERE source='outlook' AND lower(from_email) IN ({ph}) AND received_at >= ?
    ORDER BY received_at""", (*owners, since)).fetchall()

logged = {r[0] for r in c.execute(
    "SELECT matched_message_id FROM send_log WHERE matched_message_id IS NOT NULL")}

ncorr = {}
for tid, corr in c.execute(
        "SELECT thread_id, correspondents FROM thread_status WHERE source='outlook'"):
    try:
        ncorr[tid] = len(json.loads(corr)) if corr else 0
    except Exception:
        ncorr[tid] = (corr or "").count("@")

unlogged = [s for s in sent if s[0] not in logged]
risky = [s for s in sent if ncorr.get(s[4], 0) > 2]

print(f"sends since {since}: {len(sent)}")
print(f"  unlogged (no send_log row): {len(unlogged)}")
print(f"  onto threads with >2 correspondents: {len(risky)}")

if risky:
    print("\nCrowded threads -- confirm each recipient was the intended one:")
    for pid, when, to, subj, tid in risky:
        print(f"  {when}  thread {tid} [{ncorr.get(tid)} correspondents]")
        print(f"      to: {(to or '')[:66]}")
        print(f"      re: {subj[:66]}")

sys.exit(1 if unlogged else 0)
