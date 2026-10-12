#!/usr/bin/env python3
"""Pull "One Cool Thing" videos from the Apps Script inbox onto ces-server.

For each submission not yet in videos/manifest.jsonl:
  list -> fetch -> decode -> verify sha256 (API == metadata == computed) and
  bytes -> write videos/<id>.<ext> + <id>.json atomically -> append manifest
  row -> ONLY THEN delete from Drive (id + sha256, require {ok:true}).
An id already in the manifest whose file exists with matching sha256 just
gets its delete retried (crash recovery).

Server API (POST JSON, Content-Type text/plain, all with "secret"):
  {action:"list"}                -> {ok:true, items:[{id, json:{metadata}}]}
  {action:"fetch", id}           -> video modes: {ok:true, id, mode, mime, sha256, video_b64}
                                    text mode:   {ok:true, id, mode:"text", sha256, text}
  {action:"delete", id, sha256}  -> {ok:true}
  any failure                    -> {ok:false, error}
Metadata json (contract v1 and v2): name, email, institution, consent, submitted_at,
received_at, bytes, sha256, optional v, user_agent, consent_text (default ""), and
mode ("video" default | "transcript_only" | "text"). Video modes also need mime and
duration_s (and must not carry text). Text mode needs text (1..2000 chars) and must
not carry mime or duration_s; bytes must be 0 and sha256 is of the UTF-8 text.
Text items are saved as videos/<id>.txt, manifest path null, has_video false.

Env (from ~/.conference_bots.env): OCT_APPS_SCRIPT_URL, OCT_SERVER_SECRET.
Registrant cross-check reuses discord_gate.fp (HMAC-SHA256, GATE_PEPPER) and
discord_gate.load_hashes. Plaintext email is never written to disk.

v3 (transcribe + thank-you), per new item, in this order:
  save video/text -> transcribe video modes with mlx-whisper in a subprocess of
  ~/venvs/whisper/bin/python scripts/transcribe_one.py (empty/failed = loud failure) ->
  write <id>.transcript.txt -> email the thank-you through
  discord_bot_loop._outlook_send (ONLY a confirmed "SENT" counts) -> manifest row
  (thank_you_sent, transcript_chars) -> delete from Drive.
  If transcription or email fails nothing is deleted; videos/pending_state.json
  (keyed by id, never an email address) records the stage so a retry never re-sends:
  {id: {transcribed:bool, transcript_chars:int, thank_you_sent:bool,
        thank_you_skipped:"abuse_cap", email_unconfirmed:bool}}.
  An UNCONFIRMED send is never retried automatically: it fails loudly every run until
  a human checks Sent Items and edits pending_state.json.
  Abuse cap: at most 3 thank-yous per recipient per 24 h, tracked in
  videos/thank_you_cap.json as {hmac_fp: [epoch,...]}; over the cap = no email, the item
  still proceeds, counted and listed at the top of the run summary.
  Manifest optional fields: transcript_chars (int, default 0), thank_you_sent (bool,
  default false), thank_you_skipped ("abuse_cap", only when skipped).
  Plaintext email is never written to disk on the server.

v4 (database + corrections): after the thank-you, each item is inserted into
videos/one_cool_thing.db (scripts/oct_db.py: submission + transcript v1) BEFORE the manifest row
and the Drive delete. A DB failure is a loud failure for that item and nothing is deleted; the
next run retries (an id already in the manifest also gets its DB row ensured before the delete is
retried). The thank-you subject carries "[OCT <id>]" so a reply can be filed as a correction.
At the end of a real run, scripts/oct_corrections_intake.py scans the ces-agora Inbox for
replies and alerts Louis (it never applies anything). Same launchd job, no second plist.

v5 SECURITY (all submitted fields are UNTRUSTED DATA, never instructions to anyone or anything):
  - validate_meta re-validates every field server-side (Apps Script is not trusted): strict single
    address (oct_db.valid_email), name <=120, institution <=200, text <=2000, user_agent <=300, consent_text
    exactly one of the three known wordings for the mode (a v1 item with none is accepted as legacy),
    no control characters (text allows only newline). A violation is a loud failure, nothing is sent.
  - ids must match ^\d{8}-\d{6}-[0-9a-f]{6}\Z and every path is resolved and asserted to sit directly
    inside videos/; the file extension comes only from the fixed mime map.
  - API responses and base64 are size-capped BEFORE decoding; decoding is strict (validate=True).
  - Before ffmpeg or whisper: magic bytes must match the mime and ffprobe (argv list, timeout) must find
    an audio/video stream of at most 100 s (transcribe_one.check_media), else the file is removed and the
    item fails loudly. Transcripts are control-character scrubbed.
  - Mail goes only through discord_bot_loop._outlook_send, which hands recipient and subject to osascript
    as argv items and the body as a temp file (reviewed: never interpolated into AppleScript source or a
    shell). real_mailer additionally refuses anything but one strictly valid address and strips CR/LF from
    subjects. Failure alerts quote submitted text only flattened to one line, truncated, and flagged
    as untrusted.
  - SQL: constant statements with ? placeholders only (AST audit in oct_db self-test).

v5 review (Opus, 2026-10-12):
  - Global cap on thank-yous to NON-registrant addresses: at most NONREG_CAP_MAX per 24 h across all
    addresses (key "_nonregistrant_global" in thank_you_cap.json, a list of epochs). Without it the form
    is a relay: anyone can make ces-agora mail attacker text to any number of distinct addresses. Over the
    cap = no email (thank_you_skipped "abuse_cap_nonregistrant"), item still proceeds, and the run FAILS
    loudly (exit 1, alert) so Louis sees it.
  - Media the ffprobe gate rejects is QUARANTINED: moved to videos/quarantine/<id>.<ext> (+ <id>.json, no
    email), deleted from Drive, no thank-you, loud failure once. Before this, a crafted file that passed the
    Apps Script magic check failed every 10 minutes forever (re-download, alert email each run) and held a
    Drive inbox slot.
  - Any item that fails a deterministic step (HOLD_STEPS) HOLD_AFTER runs in a row is HELD: skipped on
    later runs, listed on a NOTICE line every run, alerted once when it is held. Release by removing
    "held" (and "fail_count") from its entry in videos/pending_state.json.
    pending_state entries gain optional fields: fail_count:int, held:str (step), quarantined:str.
  - Disk guard: no item is processed while free space under videos/ is below MIN_FREE_BYTES (loud).
  - Lone UTF-16 surrogates in any metadata string, and bidi/zero-width format characters in name or
    institution, are rejected (they could not be mailed or stored, or they disguise a name).

Exit codes: 0 ok, 1 one or more failures (alert emailed), 2 config missing.
Loud failure: no fallbacks, no coercion. Python 3.9 compatible.

  --dry-run    list only, no fetch/delete/writes
  --self-test  run against an in-process fake API
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import shutil
import json
import logging
import os
import re
import sys
import subprocess
import tempfile
import time
import sqlite3
import urllib.request
import oct_db
import transcribe_one
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

LOG_PATH = os.path.join(HERE, "logs", "one_cool_thing.log")
SOURCE = "apps-script-v1"
HTTP_TIMEOUT_S = 180
EXT = {"video/webm": "webm", "video/mp4": "mp4"}
WHISPER_PY = os.path.expanduser("~/venvs/whisper/bin/python")
TRANSCRIBE_HELPER = os.path.join(HERE, "transcribe_one.py")
TRANSCRIBE_TIMEOUT_S = 300
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_B64_CHARS = (MAX_VIDEO_BYTES + 2) // 3 * 4 + 4
MAX_RESPONSE_BYTES = MAX_B64_CHARS + 1024 * 1024      # a fetch reply is JSON around one base64 video
CAP_MAX = 3
CAP_WINDOW_S = 24 * 3600
THANKS_SUBJECT = "Thank you for sharing one cool thing"     # + " [OCT <id>]" (v4: replies are matched on the tag)
NONREG_CAP_MAX = 30                     # thank-yous to non-registrant addresses, all addresses together, per 24 h
NONREG_CAP_KEY = "_nonregistrant_global"   # cannot collide with an HMAC hex fingerprint
HOLD_AFTER = 3                          # consecutive deterministic failures before an item is held
HOLD_STEPS = ("validate", "verify", "decode", "media", "transcribe", "unexpected")
MIN_FREE_BYTES = 20 * 1024 ** 3
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_FORMAT_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")

log = logging.getLogger("onecoolthing")

MODES = ("video", "transcript_only", "text")
MAX_TEXT_CHARS = 2000
# field -> (types, required) for every mode
META_COMMON = {
    "name": (str, True), "email": (str, True), "institution": (str, True),
    "consent": (bool, True), "user_agent": (str, False), "submitted_at": (str, True),
    "received_at": (str, True), "bytes": (int, True), "sha256": (str, True),
    "v": (int, False), "consent_text": (str, False), "mode": (str, False),
}
META_VIDEO = {"mime": (str, True), "duration_s": ((int, float), True)}
META_TEXT = {"text": (str, True)}


class Fail(Exception):
    def __init__(self, step, msg):
        super().__init__("%s: %s" % (step, msg))
        self.step, self.msg = step, msg


def banner(text):
    line = "=" * 70
    print("\n%s\nFAILED: %s\n%s" % (line, text, line), file=sys.stderr)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_hex(b):
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------ HTTP
def api(cfg, action, **kw):
    """POST one action; one retry; raise Fail unless response is {ok:true}."""
    body = json.dumps(dict(action=action, secret=cfg["secret"], **kw)).encode()
    last = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                cfg["url"], data=body, method="POST",
                headers={"Content-Type": "text/plain;charset=utf-8"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                raw = r.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("API response larger than %d bytes" % MAX_RESPONSE_BYTES)
            res = json.loads(raw.decode("utf-8"))
            if not isinstance(res, dict) or res.get("ok") is not True:
                err = res.get("error") if isinstance(res, dict) else raw[:200]
                raise ValueError("API said not ok: %s" % str(err)[:200])
            return res
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:300])
            log.warning("%s attempt %d failed: %s", action, attempt, last)
            if attempt == 1:
                time.sleep(cfg.get("retry_sleep", 3))
    raise Fail(action, last)


# ------------------------------------------------------------- validation
def meta_mode(meta):
    return meta.get("mode", "video")


def validate_meta(meta, item_id):
    if not isinstance(meta, dict):
        raise Fail("validate", "metadata for %s is not an object" % item_id)
    mode = meta_mode(meta)
    if not isinstance(mode, str) or mode not in MODES:
        raise Fail("validate", "mode %r not allowed" % (mode,))
    fields = dict(META_COMMON)
    fields.update(META_TEXT if mode == "text" else META_VIDEO)
    for k, (typ, req) in fields.items():
        if k not in meta:
            if req:
                raise Fail("validate", "metadata missing field %r" % k)
            continue
        v = meta[k]
        if isinstance(v, bool) and typ is not bool:
            raise Fail("validate", "field %r is bool, expected %s" % (k, typ))
        if not isinstance(v, typ):
            raise Fail("validate", "field %r has type %s" % (k, type(v).__name__))
    forbidden = ("mime", "duration_s") if mode == "text" else ("text",)
    for k in forbidden:
        if k in meta:
            raise Fail("validate", "field %r is not allowed in mode %s" % (k, mode))
    if meta["consent"] is not True:
        raise Fail("validate", "consent is not true")
    for k, v in meta.items():
        if isinstance(v, str) and _SURROGATE_RE.search(v):
            raise Fail("validate", "%s: field %r contains a lone surrogate (not encodable)" % (item_id, k))
    for k in ("name", "institution"):
        if _FORMAT_RE.search(meta[k]):
            raise Fail("validate", "%s: field %r contains bidi or zero-width format characters" % (item_id, k))
    try:
        if not oct_db.valid_email(meta["email"]):
            raise oct_db.DbError("email is not one strictly valid address")
        oct_db.check_line(meta["name"], oct_db.MAX_NAME, "name")
        oct_db.check_line(meta["institution"], oct_db.MAX_INSTITUTION, "institution", allow_empty=True)
        oct_db.check_line(meta.get("user_agent", ""), oct_db.MAX_UA, "user_agent", allow_empty=True)
        oct_db.check_line(meta["submitted_at"], 64, "submitted_at")
        oct_db.check_line(meta["received_at"], 64, "received_at")
        if "consent_text" in meta:
            if meta["consent_text"] != oct_db.CONSENT_BY_MODE[mode]:
                raise oct_db.DbError("consent_text is not the known wording for mode %s" % mode)
        elif meta.get("v") != 1:
            raise oct_db.DbError("consent_text is required for v2 submissions")
        if not (0 <= meta["bytes"] <= MAX_VIDEO_BYTES):
            raise oct_db.DbError("bytes %r out of range" % meta["bytes"])
        if mode == "text":
            oct_db.check_multiline(meta["text"], MAX_TEXT_CHARS, "text")
    except oct_db.DbError as exc:
        raise Fail("validate", "%s: %s" % (item_id, exc))
    if len(meta["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in meta["sha256"]):
        raise Fail("validate", "metadata sha256 malformed")
    if mode == "text":
        if not (1 <= len(meta["text"]) <= MAX_TEXT_CHARS):
            raise Fail("validate", "text length %d out of range 1..%d" % (len(meta["text"]), MAX_TEXT_CHARS))
        if meta["bytes"] != 0:
            raise Fail("validate", "text item has bytes %r, expected 0" % meta["bytes"])
        return
    if meta["mime"] not in EXT:
        raise Fail("validate", "mime %r not allowed" % meta["mime"])
    if not (0 < meta["duration_s"] <= 95):
        raise Fail("validate", "duration_s %r out of range" % meta["duration_s"])


ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}\Z")    # self-test swaps in short fake ids, see self_test()


def safe_id(item_id):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        raise Fail("validate", "id %r does not match yyyyMMdd-HHmmss-6hex" % (item_id,))
    return item_id


def inside(vdir, path):
    """Resolve path and require it to sit directly inside vdir (no traversal, no symlink escape)."""
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(vdir):
        raise Fail("validate", "path %s is outside %s" % (path, vdir))
    return path


# --------------------------------------------------------------- registrants
def make_matcher(hashes_path=None, form_path=None, pepper=None):
    """Return fn(email)->bool using discord_gate.fp / load_hashes."""
    import discord_gate as dg
    pepper = pepper if pepper is not None else os.environ.get("GATE_PEPPER", "")
    if not pepper:
        raise Fail("config", "GATE_PEPPER not set; cannot cross-check registrants")
    hashes = dg.load_hashes(hashes_path, form_path)
    if not hashes:
        raise Fail("config", "gate hashes empty or missing; cannot cross-check")
    return lambda email: dg.fp(pepper, email) in hashes


# ------------------------------------------------------------------ files
def atomic_write(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(path):
    rows = {}
    if os.path.exists(path):
        with open(path) as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    rows[row["id"]] = row
                except Exception as exc:
                    raise Fail("manifest", "line %d unreadable: %s" % (n, exc))
    return rows


def append_manifest(path, row):
    with open(path, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ------------------------------------------------------- v3: state, transcribe, mail
def load_state(path, what):
    """Strict read of a small JSON dict; a corrupt file is a loud failure, never reset."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        return obj
    except Exception as exc:
        raise Fail("state", "%s unreadable (%s): %s" % (what, path, exc))


def save_state(path, obj):
    atomic_write(path, json.dumps(obj, indent=1, sort_keys=True).encode())


def transcribe_with_helper(media_path, python=None, helper=None, timeout=None):
    """Run the whisper helper in the whisper venv. Returns non-empty text or raises Fail."""
    cmd = [python or WHISPER_PY, helper or TRANSCRIBE_HELPER, media_path]      # argv list, shell never involved
    env = dict(os.environ, PATH=TOOL_PATH)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           timeout=timeout or TRANSCRIBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise Fail("transcribe", "timed out after %ss" % (timeout or TRANSCRIBE_TIMEOUT_S))
    except OSError as exc:
        raise Fail("transcribe", "cannot start %s: %s" % (cmd[0], exc))
    lines = (r.stdout or "").strip().splitlines()
    try:
        out = json.loads(lines[-1])
    except Exception:
        raise Fail("transcribe", "no JSON from helper (rc=%s): %s" % (
            r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-300:]))
    if r.returncode != 0 or not isinstance(out, dict) or out.get("ok") is not True:
        raise Fail("transcribe", "helper failed (rc=%s): %s" % (r.returncode, str(out.get("error") if isinstance(out, dict) else out)[:300]))
    text = out.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Fail("transcribe", "empty transcript")
    return text.strip()


def real_mailer(to, subject, body):
    """Existing registrant-mail path: discord_bot_loop._outlook_send (Outlook on
    ces-agora). Returns 'SENT' or 'UNCONFIRMED'; raises RuntimeError when nothing sent.
    It appends its own 'Best,' + ces signature, so the body carries no closing."""
    if not oct_db.valid_email(to):
        raise RuntimeError("refusing to send: recipient is not one strictly valid address")
    subject = " ".join(subject.replace("\r", " ").replace("\n", " ").split())[:200]
    if not subject:
        raise RuntimeError("refusing to send: empty subject")
    import discord_bot_loop as dbl      # _outlook_send: recipient+subject as osascript argv, body as a temp file
    return dbl._outlook_send([to], subject, body)


def thank_you_subject(item_id):
    safe_id(item_id)
    return "%s [OCT %s]" % (THANKS_SUBJECT, item_id)


def thank_you_body(mode, name, text):
    if mode == "text":
        return ("Dear %s,\n\nThank you for sharing one cool thing from the AI and History Conference. "
                "Here is what you wrote:\n\n\"%s\"\n\n"
                "If anything looks wrong, just reply to this email with the correction. "
                "If you would like your submission removed, email lhyman6@jh.edu.\n" % (name, text))
    extra = ""
    if mode == "transcript_only":
        extra = "As you asked, we will only use your words, never the video itself.\n\n"
    return ("Dear %s,\n\nThank you for sharing one cool thing from the AI and History Conference.\n\n"
            "Here is the transcript of your recording:\n\n\"%s\"\n\n"
            "The transcript was made by a private, local LLM running on our own server at Johns Hopkins. "
            "Your recording was not sent to any cloud service.\n\n%s"
            "If anything in the transcript looks wrong, just reply to this email with the correction. "
            "If you would like your submission removed, email lhyman6@jh.edu.\n" % (name, text, extra))


def thank_you(cfg, vdir, item_id, meta, mode, text, pend, pstate, counts, registrant=True):
    """Send (or skip by cap, or recognise as already sent). Returns True if sent, False if
    skipped. Raises Fail unless the outcome is a confirmed SENT or a counted cap skip."""
    if pend.get("thank_you_sent") is True:
        return True
    if pend.get("thank_you_skipped"):
        return False
    if pend.get("email_unconfirmed"):
        raise Fail("email", "an earlier send for this id was UNCONFIRMED (may be queued in the "
                   "ces-agora Outbox); not resending. Check Sent Items, then edit %s" % os.path.join(vdir, "pending_state.json"))
    cap_path = os.path.join(vdir, "thank_you_cap.json")
    cap = load_state(cap_path, "abuse cap file")
    if not oct_db.valid_email(meta["email"]):
        raise Fail("email", "%s: address is not one strictly valid address; nothing sent" % item_id)
    key = cfg["fp"](meta["email"])
    now = time.time()
    recent = [t for t in cap.get(key, []) if now - t < CAP_WINDOW_S]
    if len(recent) >= CAP_MAX:
        pend["thank_you_skipped"] = "abuse_cap"
        pstate[item_id] = pend
        save_state(os.path.join(vdir, "pending_state.json"), pstate)
        counts["cap_skipped"].append(item_id)
        log.warning("%s thank-you SKIPPED by abuse cap (%d in 24h)", item_id, len(recent))
        return False
    g = cap.get(NONREG_CAP_KEY, [])
    if not isinstance(g, list):
        raise Fail("state", "abuse cap file: %s is not a list" % NONREG_CAP_KEY)
    g_recent = [t for t in g if now - t < CAP_WINDOW_S]
    if not registrant and len(g_recent) >= NONREG_CAP_MAX:
        pend["thank_you_skipped"] = "abuse_cap_nonregistrant"
        pstate[item_id] = pend
        save_state(os.path.join(vdir, "pending_state.json"), pstate)
        counts["cap_skipped"].append(item_id)
        counts["global_cap"].append(item_id)
        log.warning("%s thank-you SKIPPED by the global non-registrant cap (%d in 24h)", item_id, len(g_recent))
        return False
    try:
        outcome = cfg["mailer"](meta["email"], thank_you_subject(item_id), thank_you_body(mode, meta["name"], text))
    except Exception as exc:
        log.error("EMAIL FAILED thank-you %s: %s", item_id, oct_db.oneline(exc, 200))
        raise Fail("email", "send failed, nothing sent: %s" % oct_db.oneline(exc, 200))
    if outcome == "SENT":
        pend["thank_you_sent"] = True
        pstate[item_id] = pend
        save_state(os.path.join(vdir, "pending_state.json"), pstate)   # before any delete
        cap[key] = recent + [now]
        if not registrant:
            cap[NONREG_CAP_KEY] = g_recent + [now]
        save_state(cap_path, cap)
        log.info("%s thank-you SENT", item_id)
        return True
    if outcome == "UNCONFIRMED":
        pend["email_unconfirmed"] = True
        pstate[item_id] = pend
        save_state(os.path.join(vdir, "pending_state.json"), pstate)
        log.error("EMAIL UNCONFIRMED thank-you %s", item_id)
        raise Fail("email", "send UNCONFIRMED (may be queued); will not resend automatically")
    raise Fail("email", "unexpected send outcome %r" % (outcome,))


# ------------------------------------------------------------ v4: database
def db_path(cfg):
    return cfg.get("db") or os.path.join(os.path.dirname(cfg["manifest"]), "one_cool_thing.db")


def record_db(cfg, row, meta, text):
    """Insert submission + transcript v1 (idempotent). Any failure is a loud Fail("db")."""
    import oct_db
    drow = {
        "id": row["id"], "mode": row["mode"], "name": row["name"], "institution": row["institution"],
        "email_fp": cfg["fp"](meta["email"]), "email_hash_match": row["email_hash_match"],
        "consent": row["consent"], "consent_text": row.get("consent_text", ""),
        "duration_s": row.get("duration_s"), "mime": row.get("mime"), "bytes": row["bytes"],
        "sha256": row["sha256"], "path": row.get("path"), "submitted_at": row["submitted_at"],
        "received_at": row["received_at"], "pulled_at": row["pulled_at"],
        "thank_you_sent": bool(row.get("thank_you_sent", False)), "source": row["source"],
    }
    src = "typed" if row["mode"] == "text" else oct_db.WHISPER_SOURCE
    try:
        conn = oct_db.connect(db_path(cfg), create=True)
        try:
            oct_db.insert_submission(conn, drow, text, src)
        finally:
            conn.close()
    except (oct_db.DbError, sqlite3.Error, OSError, KeyError) as exc:
        log.error("DB FAILED %s: %s", row.get("id"), str(exc)[:200])
        raise Fail("db", "could not record %s in the database: %s: %s" % (
            row.get("id"), type(exc).__name__, str(exc)[:200]))


# ------------------------------------------------------------ quarantine / hold
def quarantine(cfg, vdir, item_id, vpath, local_meta, sha, exc):
    """Media the gate rejected: keep it locally (never processed), delete it from Drive, fail loudly once."""
    qdir = os.path.join(vdir, "quarantine")
    os.makedirs(qdir, mode=0o700, exist_ok=True)
    qpath = inside(qdir, os.path.join(qdir, os.path.basename(vpath)))
    os.replace(vpath, qpath)
    qmeta = dict(local_meta, quarantined_reason=oct_db.oneline(exc, 300), quarantined_at=now_iso())
    atomic_write(inside(qdir, os.path.join(qdir, item_id + ".json")), json.dumps(qmeta, indent=1, sort_keys=True).encode())
    pend_path = os.path.join(vdir, "pending_state.json")
    pstate = load_state(pend_path, "pending state")
    pstate[item_id] = {"quarantined": "media"}
    save_state(pend_path, pstate)
    api(cfg, "delete", id=item_id, sha256=sha)
    log.error("%s QUARANTINED (media rejected) and deleted from Drive", item_id)
    raise Fail("media", "rejected before ffmpeg/whisper: %s. QUARANTINED to %s and deleted from Drive; "
               "no thank-you sent" % (exc, qpath))


def note_failure(vdir, item_id, step):
    """Count a deterministic failure; hold the item after HOLD_AFTER of them. Returns a note or ''."""
    if step not in HOLD_STEPS or not isinstance(item_id, str) or not ID_RE.match(item_id):
        return ""
    pend_path = os.path.join(vdir, "pending_state.json")
    pstate = load_state(pend_path, "pending state")
    e = pstate.get(item_id, {})
    if not isinstance(e, dict):
        raise Fail("state", "pending_state entry for %s is not an object" % item_id)
    e["fail_count"] = int(e.get("fail_count", 0)) + 1
    note = ""
    if e["fail_count"] >= HOLD_AFTER and not e.get("held"):
        e["held"] = step
        note = (" | HELD after %d failures: skipped from now on (still in Drive). Release by removing 'held' "
                "and 'fail_count' for this id in %s" % (e["fail_count"], pend_path))
    pstate[item_id] = e
    save_state(pend_path, pstate)
    return note


def is_held(vdir, item_id):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        return False
    e = load_state(os.path.join(vdir, "pending_state.json"), "pending state").get(item_id)
    return isinstance(e, dict) and bool(e.get("held"))


# ------------------------------------------------------------ one item
def process_item(cfg, item, manifest, matcher, vdir, counts):
    item_id = safe_id(item.get("id") if isinstance(item, dict) else None)
    meta = item.get("json")
    validate_meta(meta, item_id)
    want = meta["sha256"]
    mode = meta_mode(meta)

    if item_id in manifest:
        row = manifest[item_id]
        rel = row.get("path")
        if rel is None:     # text item: local copy is videos/<id>.txt
            path = os.path.join(vdir, item_id + ".txt")
        else:
            path = os.path.join(ROOT, rel) if not os.path.isabs(rel) else rel
        inside(vdir, path)
        if not os.path.exists(path):
            raise Fail("recover", "in manifest but file missing: %s" % path)
        with open(path, "rb") as fh:
            if sha_hex(fh.read()) != row["sha256"] or row["sha256"] != want:
                raise Fail("recover", "in manifest but local file sha256 differs; NOT deleting")
        tpath = inside(vdir, os.path.join(vdir, item_id + (".txt" if rel is None else ".transcript.txt")))
        if not os.path.exists(tpath):
            raise Fail("recover", "in manifest but transcript file missing: %s" % tpath)
        with open(tpath, encoding="utf-8") as fh:
            record_db(cfg, row, meta, fh.read().strip())      # ensure the DB row before the delete
        api(cfg, "delete", id=item_id, sha256=want)
        pstate = load_state(os.path.join(vdir, "pending_state.json"), "pending state")
        if pstate.pop(item_id, None) is not None:
            save_state(os.path.join(vdir, "pending_state.json"), pstate)
        counts["had"] += 1
        log.info("%s already pulled; delete retried OK", item_id)
        return

    res = api(cfg, "fetch", id=item_id)
    api_sha = res.get("sha256")
    if not isinstance(api_sha, str):
        raise Fail("fetch", "response lacks sha256 string")
    if "mode" in res and res["mode"] != mode:
        raise Fail("fetch", "response mode %r differs from metadata mode %r" % (res["mode"], mode))
    local_meta = {k: v for k, v in meta.items() if k != "email"}
    local_meta["mode"] = mode
    local_meta.setdefault("consent_text", "")
    local_meta["email_hash_match"] = bool(matcher(meta["email"]))
    row = {
        "id": item_id, "name": meta["name"], "email_hash_match": local_meta["email_hash_match"],
        "institution": meta["institution"], "consent": meta["consent"],
        "consent_text": local_meta["consent_text"], "mode": mode,
        "submitted_at": meta["submitted_at"], "received_at": meta["received_at"],
        "source": SOURCE,
    }
    if mode == "text":
        text = res.get("text")
        if not isinstance(text, str):
            raise Fail("fetch", "response lacks text string")
        if text != meta["text"]:
            raise Fail("verify", "fetched text differs from metadata text")
        data = text.encode("utf-8")
        got = sha_hex(data)
        if not (got == api_sha == want):
            raise Fail("verify", "sha256 mismatch computed=%s api=%s meta=%s" % (got, api_sha, want))
        tpath = inside(vdir, os.path.join(vdir, item_id + ".txt"))
        atomic_write(tpath, data)
        atomic_write(inside(vdir, os.path.join(vdir, item_id + ".json")),
                     json.dumps(local_meta, indent=1, sort_keys=True).encode())
        row.update({"duration_s": None, "mime": None, "bytes": 0, "sha256": got,
                    "path": None, "has_video": False, "text_chars": len(text)})
        media_path, mail_text = None, text
    else:
        b64 = res.get("video_b64")
        if not isinstance(b64, str):
            raise Fail("fetch", "response lacks video_b64 string")
        if len(b64) > MAX_B64_CHARS:
            raise Fail("decode", "base64 payload of %d chars exceeds the cap %d" % (len(b64), MAX_B64_CHARS))
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise Fail("decode", str(exc)[:200])
        if not data or len(data) > MAX_VIDEO_BYTES:
            raise Fail("decode", "decoded size %d outside 1..%d" % (len(data), MAX_VIDEO_BYTES))
        if not transcribe_one.magic_ok(data[:16], meta["mime"]):
            raise Fail("media", "magic bytes do not match mime %s; nothing written" % meta["mime"])
        got = sha_hex(data)
        if not (got == api_sha == want):
            raise Fail("verify", "sha256 mismatch computed=%s api=%s meta=%s" % (got, api_sha, want))
        if len(data) != meta["bytes"]:
            raise Fail("verify", "bytes mismatch got=%d meta=%d" % (len(data), meta["bytes"]))
        vpath = inside(vdir, os.path.join(vdir, "%s.%s" % (item_id, EXT[meta["mime"]])))
        atomic_write(vpath, data)
        try:
            cfg["prober"](vpath, meta["mime"])       # ffprobe: real audio/video stream, duration <= 100 s
        except transcribe_one.MediaRejected as exc:
            quarantine(cfg, vdir, item_id, vpath, local_meta, got, exc)
        atomic_write(inside(vdir, os.path.join(vdir, item_id + ".json")),
                     json.dumps(local_meta, indent=1, sort_keys=True).encode())
        row.update({"duration_s": meta["duration_s"], "mime": meta["mime"], "bytes": len(data),
                    "sha256": got, "path": os.path.relpath(vpath, ROOT),
                    "has_video": True, "text_chars": 0})
        media_path = vpath
        mail_text = None
    pend_path = os.path.join(vdir, "pending_state.json")
    pstate = load_state(pend_path, "pending state")
    pend = pstate.get(item_id, {})
    if not isinstance(pend, dict):
        raise Fail("state", "pending_state entry for %s is not an object" % item_id)
    if media_path is not None:                       # transcribe video modes
        tpath = inside(vdir, os.path.join(vdir, item_id + ".transcript.txt"))
        if pend.get("transcribed") is True and os.path.exists(tpath):
            with open(tpath, encoding="utf-8") as fh:
                mail_text = fh.read()
            if len(mail_text) != pend.get("transcript_chars") or not mail_text.strip():
                raise Fail("transcribe", "saved transcript for %s does not match pending_state" % item_id)
        else:
            mail_text = cfg["transcriber"](media_path)
            if not isinstance(mail_text, str) or not mail_text.strip():
                raise Fail("transcribe", "empty transcript")
            mail_text = oct_db.scrub_multiline(mail_text).strip()      # whisper output is data too
            if not mail_text:
                raise Fail("transcribe", "empty transcript")
            atomic_write(tpath, mail_text.encode("utf-8"))
            pend.update({"transcribed": True, "transcript_chars": len(mail_text)})
            pstate[item_id] = pend
            save_state(pend_path, pstate)
        row["transcript_chars"] = len(mail_text)
    else:
        row["transcript_chars"] = 0
    row["thank_you_sent"] = thank_you(cfg, vdir, item_id, meta, mode, mail_text, pend, pstate, counts,
                                      registrant=row["email_hash_match"])
    if not row["thank_you_sent"]:
        row["thank_you_skipped"] = pend.get("thank_you_skipped", "abuse_cap")
    row["pulled_at"] = now_iso()
    record_db(cfg, row, meta, mail_text)               # DB failure: loud, nothing deleted, retried next run
    append_manifest(cfg["manifest"], row)
    manifest[item_id] = row
    counts["pulled"] += 1
    counts["matches"] += 1 if row["email_hash_match"] else 0
    log.info("%s pulled (mode=%s, %d bytes, registrant=%s)", item_id, mode, len(data), row["email_hash_match"])
    api(cfg, "delete", id=item_id, sha256=got)    # only after local write, transcript, thank-you, manifest
    log.info("%s deleted from Drive", item_id)
    if pstate.pop(item_id, None) is not None:
        save_state(pend_path, pstate)


# ------------------------------------------------------------------ main run
def run(cfg, dry=False, matcher=None, alert=None):
    counts = dict(pulled=0, had=0, failed=0, matches=0, cap_skipped=[], global_cap=[], held=[])
    failures = []
    try:
        items = api(cfg, "list").get("items")
        if not isinstance(items, list):
            raise Fail("list", "response has no items list")
    except Fail as f:
        failures.append(("-", f.step, f.msg))
        items = []
    if dry:
        print("DRY RUN: %d item(s) in inbox: %s" % (
            len(items), ", ".join(oct_db.oneline(i.get("id"), 40) for i in items if isinstance(i, dict))))
        for f in failures:
            banner("list: %s" % f[2])
        return 1 if failures else 0
    vdir = os.path.dirname(cfg["manifest"])
    os.makedirs(vdir, exist_ok=True)
    try:
        manifest = read_manifest(cfg["manifest"])
        if items and matcher is None:
            matcher = make_matcher()
    except Fail as f:
        failures.append(("-", f.step, f.msg))
        items = []
        manifest = {}
    if items:
        free = shutil.disk_usage(vdir).free
        if free < cfg.get("min_free_bytes", MIN_FREE_BYTES):
            failures.append(("-", "disk", "only %.1f GB free under %s; nothing processed, everything stays in Drive"
                             % (free / 1024.0 ** 3, vdir)))
            items = []
    for item in items:
        iid = item.get("id") if isinstance(item, dict) else None
        try:
            if is_held(vdir, iid):
                counts["held"].append(iid)
                continue
        except Fail as f:
            failures.append((oct_db.oneline(iid, 60), f.step, oct_db.oneline(f.msg, 300)))
            continue
        step, msg = None, None
        try:
            process_item(cfg, item, manifest, matcher, vdir, counts)
        except Fail as f:
            step, msg = f.step, f.msg
        except Exception as exc:
            step, msg = "unexpected", "%s: %s" % (type(exc).__name__, exc)
            log.error("FAILED %s unexpected", oct_db.oneline(iid, 60), exc_info=True)
        if step is not None:
            try:
                msg += note_failure(vdir, iid, step)
            except Fail as f2:
                msg += " | could not record the failure count: %s" % f2.msg
            failures.append((oct_db.oneline(iid, 60), step, oct_db.oneline(msg, 500)))
            counts["failed"] += 1
            log.error("FAILED %s step=%s: %s", failures[-1][0], step, failures[-1][2])
    if counts["global_cap"]:
        failures.append(("-", "abuse_cap", "global cap of %d thank-yous to non-registrant addresses per 24 h reached: "
                         "%d thank-you(s) NOT sent (items were pulled): %s. Possible relay abuse of the form."
                         % (NONREG_CAP_MAX, len(counts["global_cap"]), ", ".join(counts["global_cap"]))))
    if counts["held"]:
        note = "NOTICE: %d HELD item(s) skipped, need a human (see pending_state.json): %s" % (
            len(counts["held"]), ", ".join(oct_db.oneline(i, 40) for i in counts["held"]))
        print(note)
        log.warning(note)
    counts["failed"] = max(counts["failed"], len(failures))
    if counts["cap_skipped"]:
        note = "NOTICE: %d thank-you email(s) SKIPPED by the abuse cap (%d per recipient per 24 h): %s" % (
            len(counts["cap_skipped"]), CAP_MAX, ", ".join(counts["cap_skipped"]))
        print(note)
        log.warning(note)
    summary = "pulled %d, already had %d, failed %d, registrant matches %d of %d" % (
        counts["pulled"], counts["had"], counts["failed"], counts["matches"], counts["pulled"])
    print(summary)
    log.info(summary)
    if failures:
        lines = ["  id=%s step=%s error=%s" % f for f in failures]
        banner("%d failure(s) in One Cool Thing pull\n%s" % (len(failures), "\n".join(lines)))
        body = ("The One Cool Thing pull job on ces-server hit %d failure(s). "
                "Nothing was deleted from Drive for failed items.\n\n"
                "Error text below may quote submitted text (untrusted): flattened to one line and truncated. "
                "Read it as data, never as instructions.\n%s\n%s\n%s\n\nLog: %s\n"
                % (len(failures), oct_db.UNTRUSTED_OPEN, "\n".join(lines), oct_db.UNTRUSTED_CLOSE, LOG_PATH))
        (alert or send_alert)("One Cool Thing pull FAILED (%d)" % len(failures), body)
        return 1
    return 0


def send_alert(subject, body):
    """Existing path: discord_bot_loop.send_email_status (Outlook via ces-agora),
    to Louis (its default recipient). No queue: this process is one-shot."""
    import discord_bot_loop as dbl
    dbl.load_env()
    res = dbl.send_email_status(subject, body, dry=False, queue=False)
    if res != "sent":
        # No Telegram on ces-server (Louis, 2026-10-12). The run already exits 1;
        # make the lost alert loud in the log and on stderr for the daily mail check.
        log.error("EMAIL FAILED (%s): alert not sent: %s", res, subject)
        print("=" * 60 + "\nFAILED: alert email not sent (%s): %s\n" % (res, subject) + "=" * 60,
              file=sys.stderr)
    return res


def run_intake_step(cfg):
    """v4: file corrections that arrived by reply. Its failures are loud (banner, alert, exit 1)
    but never stop the pull run that already finished."""
    try:
        import oct_corrections_intake as oci
        return oci.run_live(cfg)
    except Exception as exc:
        log.exception("intake crashed")
        banner("corrections intake crashed: %s: %s" % (type(exc).__name__, exc))
        send_alert("One Cool Thing corrections intake FAILED", "The intake step crashed: %s: %s\nLog: %s\n" % (
            type(exc).__name__, oct_db.oneline(exc, 300), LOG_PATH))
        return 1


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH)])


def load_cfg():
    try:
        import discord_bot_loop as dbl
        dbl.load_env()
    except Exception as exc:
        banner("cannot load env via discord_bot_loop: %s" % exc)
        sys.exit(2)
    missing = [k for k in ("OCT_APPS_SCRIPT_URL", "OCT_SERVER_SECRET") if not os.environ.get(k)]
    if missing:
        banner("missing env var(s): %s (set in ~/.conference_bots.env)" % ", ".join(missing))
        sys.exit(2)
    pepper = os.environ.get("GATE_PEPPER", "")
    if not pepper:
        banner("GATE_PEPPER not set; cannot key the thank-you abuse cap")
        sys.exit(2)
    import discord_gate as dg
    return {"url": os.environ["OCT_APPS_SCRIPT_URL"], "secret": os.environ["OCT_SERVER_SECRET"],
            "manifest": os.path.join(ROOT, "videos", "manifest.jsonl"),
            "transcriber": transcribe_with_helper, "mailer": real_mailer,
            "prober": transcribe_one.check_media,
            "fp": lambda email: dg.fp(pepper, email)}


# ------------------------------------------------------------------ self-test
def self_test():
    import http.server
    import shutil
    import subprocess
    import threading

    state = {"items": {}, "deleted": [], "bad_delete": False}
    SECRET = "s3cret"
    WEBM_MAGIC = b"\x1a\x45\xdf\xa3"

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if req.get("secret") != SECRET:
                out = {"ok": False, "error": "bad secret"}
            elif req["action"] == "list":
                out = {"ok": True, "items": [{"id": i, "json": v["meta"]}
                                             for i, v in state["items"].items()]}
            elif req["action"] == "fetch":
                v = state["items"][req["id"]]
                if v["meta"].get("mode") == "text":
                    out = {"ok": True, "id": req["id"], "mode": "text", "sha256": v["api_sha"],
                           "text": v["data"].decode("utf-8")}
                else:
                    out = {"ok": True, "id": req["id"], "sha256": v["api_sha"],
                           "video_b64": v.get("override_b64") or base64.b64encode(v["data"]).decode()}
            elif req["action"] == "delete":
                if state["bad_delete"]:
                    out = {"ok": False, "error": "drive down"}
                else:
                    state["deleted"].append(req["id"])
                    state["items"].pop(req["id"], None)
                    out = {"ok": True}
            else:
                out = {"ok": False, "error": "unknown"}
            b = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def add(i, data=WEBM_MAGIC + b"fakevideo-" + os.urandom(8), api_sha=None, email="registrant-test-address@example.test"):
        h = sha_hex(data)
        state["items"][i] = {"data": data, "api_sha": api_sha or h, "meta": {
            "v": 1, "name": "T " + i, "email": email, "institution": "X", "consent": True,
            "duration_s": 30, "mime": "video/webm", "user_agent": "ua",
            "submitted_at": "2026-10-12T00:00:00Z", "received_at": "2026-10-12T00:00:01Z",
            "bytes": len(data), "sha256": h}}

    def add_text(i, text="Un historien tapé \u00e9\u2603 text", api_sha=None, extra=None,
                 email="registrant-test-address@example.test"):
        data = text.encode("utf-8")
        h = sha_hex(data)
        meta = {"v": 2, "mode": "text", "name": "T " + i, "email": email, "institution": "X",
                "consent": True, "consent_text": oct_db.CONSENT_BY_MODE["text"], "text": text,
                "user_agent": "ua", "submitted_at": "2026-10-12T00:00:00Z",
                "received_at": "2026-10-12T00:00:01Z", "bytes": 0, "sha256": h}
        meta.update(extra or {})
        state["items"][i] = {"data": data, "api_sha": api_sha or h, "meta": meta}

    def add_tr(i):
        add(i)
        state["items"][i]["meta"].update({"v": 2, "mode": "transcript_only",
                                          "consent_text": oct_db.CONSENT_BY_MODE["transcript_only"]})

    tmp = tempfile.mkdtemp(prefix="oct_selftest_")
    skipped_checks = []
    alerts = []
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        ok = ok and cond

    print("self-test: id validation is strict (real pattern)")
    import oct_db
    for bad in ("a1", "../etc/x", "20261012-135502-60950g", "20261012-135502-609502\n", "20261012-135502-609502/x", ""):
        try:
            safe_id(bad)
            check("safe_id rejects %r" % bad, False)
        except Fail:
            check("safe_id rejects %r" % bad, True)
    check("safe_id accepts a real id", safe_id("20261012-135502-609502") == "20261012-135502-609502")
    try:
        inside(tmp, os.path.join(tmp, "..", "x"))
        check("inside() rejects traversal", False)
    except Fail:
        check("inside() rejects traversal", True)
    check("inside() accepts a file in the directory", inside(tmp, os.path.join(tmp, "f.txt")))
    real_pat, real_oct_pat = ID_RE, oct_db.ID_RE
    ID_RE_swap = re.compile(r"^[a-z][a-z0-9]{1,2}\Z")   # fixtures below use short ids like a1, k0, v1a
    globals()["ID_RE"] = ID_RE_swap
    oct_db.ID_RE = ID_RE_swap

    mail = {"sent": [], "mode": "ok", "calls": 0}      # mode: ok | fail | unconfirmed
    tcalls = []
    tmode = {"v": "ok"}                                 # ok | fail | empty | text-only-forbidden

    def stub_transcriber(path):
        tcalls.append(path)
        if tmode["v"] == "fail":
            raise Fail("transcribe", "stub failure")
        if tmode["v"] == "empty":
            return "   "
        if tmode["v"] == "forbidden":
            raise AssertionError("transcriber must not run for text mode")
        return "stub transcript for " + os.path.basename(path)

    probe = {"v": "ok", "calls": []}

    def stub_prober(path, mime):
        probe["calls"].append(path)
        if probe["v"] == "reject":
            raise transcribe_one.MediaRejected("stub: not audio/video")
        return 5.0

    def stub_mailer(to, subject, body):
        mail["calls"] += 1
        if mail["mode"] == "fail":
            raise RuntimeError("stub outlook down")
        if mail["mode"] == "unconfirmed":
            return "UNCONFIRMED"
        mail["sent"].append((to, subject, body))
        return "SENT"

    def cfg(sub):
        d = os.path.join(tmp, sub)
        return {"url": "http://127.0.0.1:%d/" % srv.server_port, "secret": SECRET,
                "manifest": os.path.join(d, "videos", "manifest.jsonl"), "retry_sleep": 0,
                "transcriber": stub_transcriber, "mailer": stub_mailer, "prober": stub_prober,
                "fp": lambda e: "fp-" + sha_hex(e.encode())[:16]}

    def reset():
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        mail["sent"].clear(); mail["mode"] = "ok"; mail["calls"] = 0
        tcalls.clear(); tmode["v"] = "ok"

    matcher = lambda e: e == "registrant-test-address@example.test"
    fa = lambda s, b: alerts.append((s, b))
    global ROOT
    old_root, ROOT = ROOT, tmp
    try:
        print("self-test: happy path")
        add("a1")
        add("a2", email="stranger-test-address@example.test")
        c = cfg("happy")
        rc = run(c, matcher=matcher, alert=fa)
        rows = read_manifest(c["manifest"])
        check("exit 0", rc == 0)
        check("both pulled and deleted", sorted(state["deleted"]) == ["a1", "a2"] and len(rows) == 2)
        check("registrant match true/false", rows["a1"]["email_hash_match"] is True
              and rows["a2"]["email_hash_match"] is False)
        vd = os.path.dirname(c["manifest"])
        alltxt = "".join(open(os.path.join(vd, f)).read() for f in os.listdir(vd)
                         if f.endswith((".json", ".jsonl", ".txt")))
        check("no plaintext email on disk (manifest, json, transcripts, state, cap)",
              "test-address@example" not in alltxt)
        check("thank-yous sent to both, rows thank_you_sent true with transcript_chars",
              len(mail["sent"]) == 2 and rows["a1"]["thank_you_sent"] is True
              and rows["a1"]["transcript_chars"] == len(open(os.path.join(vd, "a1.transcript.txt")).read()) > 0)
        check("pending_state empty after success",
              json.load(open(os.path.join(vd, "pending_state.json"))) == {})
        check("video file written", os.path.getsize(os.path.join(vd, "a1.webm")) > 0)
        check("no alert", not alerts)
        import oct_db
        dbc = oct_db.connect(os.path.join(vd, "one_cool_thing.db"))
        subs = {r["id"]: r for r in dbc.execute("SELECT * FROM submissions")}
        trs = [r for r in dbc.execute("SELECT * FROM transcripts WHERE is_current=1")]
        check("DB: both submissions, one current transcript v1 each, source whisper",
              sorted(subs) == ["a1", "a2"] and len(trs) == 2
              and all(t["version"] == 1 and t["source"].startswith("whisper:") for t in trs))
        check("DB: email_fp is the HMAC, match flag matches manifest",
              subs["a1"]["email_fp"] == c["fp"]("registrant-test-address@example.test") and subs["a1"]["email_hash_match"] == 1
              and subs["a2"]["email_hash_match"] == 0 and subs["a1"]["thank_you_sent"] == 1)
        check("DB transcript equals the transcript file",
              [t["text"] for t in trs if t["submission_id"] == "a1"][0] == open(os.path.join(vd, "a1.transcript.txt")).read())
        dbc.close()
        check("no plaintext email inside the DB file",
              b"test-address@example" not in open(os.path.join(vd, "one_cool_thing.db"), "rb").read())

        print("self-test: sha mismatch must NOT delete")
        state["deleted"].clear()
        add("b1", api_sha="0" * 64)
        c = cfg("sha")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 1", rc == 1)
        check("not deleted", state["deleted"] == [] and "b1" in state["items"])
        check("no file, no manifest row", not os.path.exists(os.path.join(os.path.dirname(c["manifest"]), "b1.webm"))
              and not read_manifest(c["manifest"]))
        check("alert sent", len(alerts) == 1 and "b1" in alerts[0][1])
        state["items"].clear()

        print("self-test: delete failure alerts, keeps file, recovers later")
        alerts.clear()
        add("c1")
        state["bad_delete"] = True
        c = cfg("del")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        check("exit 1", rc == 1)
        check("file kept and manifest row present", os.path.exists(os.path.join(vd, "c1.webm"))
              and "c1" in read_manifest(c["manifest"]))
        check("alert sent", len(alerts) == 1 and "delete" in alerts[0][1])
        state["bad_delete"] = False
        alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        check("recovery run retries delete, exit 0", rc == 0 and "c1" in state["deleted"] and not alerts)

        print("self-test: manifest row present but DB row missing is repaired before the delete retry")
        os.unlink(os.path.join(vd, "one_cool_thing.db"))
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add("c1", data=b"a different video")   # same id, other bytes: recovery must refuse
        rc = run(c, matcher=matcher, alert=fa)
        check("recovery with a different video refuses to delete", rc == 1 and state["deleted"] == [])
        state["items"].clear(); alerts.clear()
        add("c1", data=open(os.path.join(vd, "c1.webm"), "rb").read())
        rc = run(c, matcher=matcher, alert=fa)
        dbc = oct_db.connect(os.path.join(vd, "one_cool_thing.db"))
        check("recovery re-creates the DB row, then deletes",
              rc == 0 and state["deleted"] == ["c1"]
              and dbc.execute("SELECT COUNT(*) FROM submissions WHERE id='c1'").fetchone()[0] == 1
              and dbc.execute("SELECT COUNT(*) FROM transcripts WHERE submission_id='c1' AND is_current=1").fetchone()[0] == 1)
        dbc.close()

        print("self-test: DB failure blocks the Drive delete, no resend, retry succeeds")
        reset()
        add("d1")
        c = cfg("dbfail")
        vd = os.path.dirname(c["manifest"])
        os.makedirs(os.path.join(vd, "bad.db"))          # a directory where the database file should be
        c["db"] = os.path.join(vd, "bad.db")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 1, NOT deleted, no manifest row", rc == 1 and state["deleted"] == [] and "d1" in state["items"]
              and not read_manifest(c["manifest"]))
        check("alert names the db step and id", len(alerts) == 1 and "db" in alerts[0][1] and "d1" in alerts[0][1])
        check("thank-you already sent once", len(mail["sent"]) == 1)
        os.rmdir(os.path.join(vd, "bad.db"))
        alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        dbc = oct_db.connect(c["db"])
        check("retry: exit 0, deleted, DB row, STILL exactly one email",
              rc == 0 and state["deleted"] == ["d1"] and len(mail["sent"]) == 1
              and dbc.execute("SELECT COUNT(*) FROM submissions WHERE id='d1'").fetchone()[0] == 1)
        dbc.close()

        print("self-test: text item is recorded as typed")
        reset()
        add_text("y1", text="typed words")
        c = cfg("textdb")
        rc = run(c, matcher=matcher, alert=fa)
        dbc = oct_db.connect(os.path.join(os.path.dirname(c["manifest"]), "one_cool_thing.db"))
        t = dbc.execute("SELECT * FROM transcripts WHERE submission_id='y1'").fetchone()
        s1 = dbc.execute("SELECT * FROM submissions WHERE id='y1'").fetchone()
        check("text: transcript source typed, text equal, path/mime NULL, subject tagged",
              rc == 0 and t["source"] == "typed" and t["text"] == "typed words" and s1["path"] is None
              and s1["mime"] is None and mail["sent"][0][1] == THANKS_SUBJECT + " [OCT y1]"
              and "If anything looks wrong, just reply to this email with the correction." in mail["sent"][0][2])
        dbc.close()

        print("self-test: v1 metadata without mode or consent_text")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add("v1a")
        check("fixture really has no mode/consent_text",
              "mode" not in state["items"]["v1a"]["meta"] and "consent_text" not in state["items"]["v1a"]["meta"])
        c = cfg("v1")
        rc = run(c, matcher=matcher, alert=fa)
        r = read_manifest(c["manifest"]).get("v1a", {})
        check("exit 0, pulled, deleted", rc == 0 and state["deleted"] == ["v1a"])
        check("row defaults: mode video, consent_text empty, has_video",
              r.get("mode") == "video" and r.get("consent_text") == "" and r.get("has_video") is True
              and r.get("text_chars") == 0 and r.get("path"))

        print("self-test: text happy path")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add_text("t1")
        c = cfg("text")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        r = read_manifest(c["manifest"]).get("t1", {})
        check("exit 0, deleted", rc == 0 and state["deleted"] == ["t1"] and not alerts)
        check("row: mode text, no video, path null, text_chars, bytes 0",
              r.get("mode") == "text" and r.get("has_video") is False and r.get("path") is None
              and r.get("text_chars") == len("Un historien tapé \u00e9\u2603 text") and r.get("bytes") == 0
              and r.get("consent_text") == oct_db.CONSENT_BY_MODE["text"] and r.get("email_hash_match") is True)
        check("txt file holds the text exactly",
              open(os.path.join(vd, "t1.txt"), "rb").read() == "Un historien tapé \u00e9\u2603 text".encode("utf-8"))
        jtxt = open(os.path.join(vd, "t1.json")).read()
        check("json has no email, has email_hash_match", "test-address@example" not in jtxt
              and '"email"' not in jtxt and '"email_hash_match": true' in jtxt)
        check("no video file", not any(f.endswith((".webm", ".mp4")) for f in os.listdir(vd)))

        print("self-test: text sha mismatch must NOT delete")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add_text("t2", api_sha="0" * 64)
        c = cfg("textsha")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        check("exit 1, not deleted", rc == 1 and state["deleted"] == [] and "t2" in state["items"])
        check("no txt, no manifest row", not os.path.exists(os.path.join(vd, "t2.txt"))
              and not read_manifest(c["manifest"]))
        check("alert sent", len(alerts) == 1 and "t2" in alerts[0][1])

        print("self-test: text mode with a video field present is rejected")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add_text("t3", extra={"mime": "video/webm"})
        add_text("t4", extra={"duration_s": 30})
        add_text("t5", text="x" * 2001)
        c = cfg("textbad")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 1, nothing deleted, nothing written",
              rc == 1 and state["deleted"] == [] and not read_manifest(c["manifest"]))
        check("alert names all three", len(alerts) == 1 and all(i in alerts[0][1] for i in ("t3", "t4", "t5")))

        print("self-test: transcript_only video path")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add_tr("p1")
        c = cfg("tr")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        r = read_manifest(c["manifest"]).get("p1", {})
        check("exit 0, deleted", rc == 0 and state["deleted"] == ["p1"])
        check("row: mode transcript_only, has_video, consent_text",
              r.get("mode") == "transcript_only" and r.get("has_video") is True and r.get("text_chars") == 0
              and r.get("consent_text") == oct_db.CONSENT_BY_MODE["transcript_only"])
        check("video file written", os.path.getsize(os.path.join(vd, "p1.webm")) > 0)

        print("self-test: video mode with text field, bad mode both rejected")
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        add("q1"); state["items"]["q1"]["meta"]["text"] = "sneaky"
        add("q2"); state["items"]["q2"]["meta"]["mode"] = "podcast"
        rc = run(cfg("badvid"), matcher=matcher, alert=fa)
        check("exit 1, nothing deleted", rc == 1 and state["deleted"] == [])

        print("self-test: transcription failure, then empty transcript")
        reset()
        add("w1")
        tmode["v"] = "fail"
        c = cfg("trfail")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        check("exit 1, not deleted, no manifest row, no email",
              rc == 1 and state["deleted"] == [] and not read_manifest(c["manifest"]) and mail["calls"] == 0)
        check("alert names step", len(alerts) == 1 and "transcribe" in alerts[0][1] and "w1" in alerts[0][1])
        tmode["v"] = "empty"; alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        check("empty transcript is a failure, no transcript file",
              rc == 1 and state["deleted"] == [] and mail["calls"] == 0
              and not os.path.exists(os.path.join(vd, "w1.transcript.txt")) and len(alerts) == 1)
        tmode["v"] = "ok"; alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        check("recovers once transcription works: one email, deleted, row",
              rc == 0 and state["deleted"] == ["w1"] and len(mail["sent"]) == 1
              and read_manifest(c["manifest"])["w1"]["thank_you_sent"] is True)

        print("self-test: real transcribe helper wrapper (fake helper)")
        fk = os.path.join(tmp, "fakehelper.py")
        def helper(code):
            open(fk, "w").write(code)
        helper("import json,sys\nprint(json.dumps({'ok':True,'text':' hello there '}))\n")
        check("ok helper returns stripped text", transcribe_with_helper("x", sys.executable, fk) == "hello there")
        for name, code in (("exit 1", "import json,sys\nprint(json.dumps({'ok':False,'error':'boom'}))\nsys.exit(1)\n"),
                           ("empty text", "import json\nprint(json.dumps({'ok':True,'text':'  '}))\n"),
                           ("garbage", "print('not json')\n"),
                           ("timeout", "import time\ntime.sleep(10)\n")):
            helper(code)
            try:
                transcribe_with_helper("x", sys.executable, fk, timeout=1)
                check("helper %s raises Fail" % name, False)
            except Fail:
                check("helper %s raises Fail" % name, True)

        print("self-test: email failure, retry sends exactly once")
        reset()
        add_tr("e1")
        mail["mode"] = "fail"
        c = cfg("mailfail")
        vd = os.path.dirname(c["manifest"])
        rc = run(c, matcher=matcher, alert=fa)
        ps = json.load(open(os.path.join(vd, "pending_state.json")))
        check("exit 1, not deleted, no manifest row", rc == 1 and state["deleted"] == []
              and not read_manifest(c["manifest"]))
        check("pending: transcribed, thank_you_sent absent", ps["e1"].get("transcribed") is True
              and "thank_you_sent" not in ps["e1"])
        check("alert names email step", len(alerts) == 1 and "email" in alerts[0][1])
        mail["mode"] = "ok"; alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        r = read_manifest(c["manifest"]).get("e1", {})
        check("retry: exit 0, exactly one email, deleted", rc == 0 and len(mail["sent"]) == 1
              and state["deleted"] == ["e1"])
        check("transcribed only once across retries", len(tcalls) == 1)
        check("row thank_you_sent true, transcript_chars > 0", r.get("thank_you_sent") is True
              and r.get("transcript_chars", 0) > 0)
        body = mail["sent"][0][2]
        check("transcript_only body: transcript, LLM paragraph, words-only line",
              '"stub transcript for e1.webm"' in body and "private, local LLM" in body
              and "never the video itself" in body and mail["sent"][0][1] == THANKS_SUBJECT + " [OCT e1]")
        check("body: reply-with-correction line and removal line, no old wording",
              "just reply to this email with the correction." in body
              and "If you would like your submission removed, email lhyman6@jh.edu." in body
              and "looks wrong, or you" not in body)
        check("body has no em dash or semicolon", "\u2014" not in body and ";" not in body)

        print("self-test: already-sent id (crash before manifest) never re-sends")
        reset()
        add("r1")
        c = cfg("resend")
        vd = os.path.dirname(c["manifest"])
        os.makedirs(vd)
        json.dump({"r1": {"transcribed": True, "transcript_chars": 5, "thank_you_sent": True}},
                  open(os.path.join(vd, "pending_state.json"), "w"))
        open(os.path.join(vd, "r1.transcript.txt"), "w").write("hello")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 0, zero emails, deleted, row thank_you_sent true",
              rc == 0 and mail["calls"] == 0 and state["deleted"] == ["r1"]
              and read_manifest(c["manifest"])["r1"]["thank_you_sent"] is True and not tcalls)

        print("self-test: UNCONFIRMED send is never retried")
        reset()
        add("u1")
        mail["mode"] = "unconfirmed"
        c = cfg("unconf")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 1, not deleted, alert", rc == 1 and state["deleted"] == [] and len(alerts) == 1)
        mail["mode"] = "ok"; alerts.clear()
        rc = run(c, matcher=matcher, alert=fa)
        check("next run fails loudly again without sending", rc == 1 and mail["calls"] == 1
              and state["deleted"] == [] and len(alerts) == 1)

        print("self-test: abuse cap (3 per recipient per 24 h)")
        reset()
        for n in range(4):
            add("k%d" % n, email="cap-test-address@example.test")
        add("k9", email="other-test-address@example.test")
        c = cfg("cap")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run(c, matcher=matcher, alert=fa)
        out = buf.getvalue().splitlines()
        rows = read_manifest(c["manifest"])
        vd = os.path.dirname(c["manifest"])
        check("exit 0, all five deleted, 4 emails sent", rc == 0 and len(state["deleted"]) == 5
              and len(mail["sent"]) == 4)
        skipped = [i for i, r in rows.items() if r.get("thank_you_sent") is False]
        check("exactly one skipped, marked abuse_cap", len(skipped) == 1
              and rows[skipped[0]].get("thank_you_skipped") == "abuse_cap")
        check("NOTICE is the first output line and names the id",
              out and out[0].startswith("NOTICE") and skipped[0] in out[0])
        capf = open(os.path.join(vd, "thank_you_cap.json")).read()
        check("cap file keyed by fp, no plaintext email", "fp-" in capf and "test-address@example" not in capf)

        print("self-test: text mode thank-you without transcription")
        reset()
        tmode["v"] = "forbidden"
        add_text("x1", text="I typed this thing")
        c = cfg("textmail")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        r = read_manifest(c["manifest"]).get("x1", {})
        body = mail["sent"][0][2] if mail["sent"] else ""
        check("exit 0, deleted, one email, no transcriber call", rc == 0 and state["deleted"] == ["x1"]
              and len(mail["sent"]) == 1 and not tcalls)
        check("text body: echo, no LLM paragraph, no em dash or semicolon",
              "Here is what you wrote:" in body and '"I typed this thing"' in body
              and "LLM" not in body and "\u2014" not in body and ";" not in body)
        check("row: thank_you_sent true, transcript_chars 0, no transcript file",
              r.get("thank_you_sent") is True and r.get("transcript_chars") == 0
              and not os.path.exists(os.path.join(vd, "x1.transcript.txt")))

        print("self-test: v5 hostile metadata is rejected loudly (nothing sent, written, or deleted)")
        reset()
        bads = {
            "h_email_comma": {"email": "a@example.test,b@example.test"},
            "h_email_semi": {"email": "a@example.test;b@example.test"},
            "h_email_crlf": {"email": "a@example.test\r\nBcc: evil@example.test"},
            "h_email_angle": {"email": "<a@example.test>"},
            "h_email_quote": {"email": "\"a\"@example.test"},
            "h_email_space": {"email": "a b@example.test"},
            "h_email_long": {"email": "a" * 250 + "@example.test"},
            "h_name_long": {"name": "n" * 121},
            "h_name_nl": {"name": "ok\nname"},
            "h_inst_long": {"institution": "i" * 201},
            "h_ua_long": {"user_agent": "u" * 301},
            "h_consent": {"consent_text": "I agree to everything"},
            "h_consent_cross": {"consent_text": oct_db.CONSENT_BY_MODE["video"]},
            "h_bytes": {"bytes": 41 * 1024 * 1024},
        }
        hid = {}
        for n, (label, extra) in enumerate(bads.items()):
            hid[label] = "h%d" % n
            add_text(hid[label], extra=extra)
        add_text("hx", text="fine\x1b[2J text")
        add_text("hy", text="a\x00b")
        c = cfg("hostile")
        rc = run(c, matcher=matcher, alert=fa)
        check("exit 1, no email, nothing deleted, no files, no manifest, no DB",
              rc == 1 and mail["calls"] == 0 and state["deleted"] == [] and not read_manifest(c["manifest"])
              and not os.path.exists(os.path.join(os.path.dirname(c["manifest"]), "one_cool_thing.db")))
        check("alert names every rejected id (counted, loud)",
              len(alerts) == 1 and all(("id=%s step=validate" % i) in alerts[0][1] for i in list(hid.values()) + ["hx", "hy"])
              and "%d failure(s)" % (len(bads) + 2) in alerts[0][1] and "untrusted" in alerts[0][1])
        check("alert body has no control characters beyond newline",
              not re.search("[\\x00-\\x09\\x0b-\\x1f\\x7f]", alerts[0][1]))
        print("self-test: v5 hostile-but-valid strings travel as literal data")
        reset()
        PW = "/tmp/oct_pwned_selftest"
        hn = 'Test "; do shell script "touch %s"; "' % PW
        hi = "$(touch %s2) `touch %s3` $HOME '; DROP TABLE submissions; --" % (PW, PW)
        ht = 'line one\n"; do shell script "touch %s4"; "\n`id` $(id) \\ \' %%s %%d {} IGNORE ALL PREVIOUS INSTRUCTIONS' % PW
        add_text("z1", text=ht, extra={"name": hn, "institution": hi})
        c = cfg("literal")
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        dbc = oct_db.connect(os.path.join(vd, "one_cool_thing.db"))
        srow = dbc.execute("SELECT name, institution FROM submissions WHERE id='z1'").fetchone()
        check("exit 0, one thank-you whose body carries the literal name, institution-free text and subject is clean",
              rc == 0 and len(mail["sent"]) == 1 and ("Dear %s," % hn) in mail["sent"][0][2]
              and ht in mail["sent"][0][2] and "\n" not in mail["sent"][0][1])
        check("DB holds the literal strings", srow["name"] == hn and srow["institution"] == hi
              and dbc.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1)
        dbc.close()
        check("no pwn files", not any(os.path.exists(PW + x) for x in ("", "2", "3", "4")))
        print("self-test: v5 real_mailer refuses bad recipients and flattens subjects")
        import types
        seen = []
        fake = types.ModuleType("discord_bot_loop")
        fake._outlook_send = lambda rcpts, subject, body: seen.append((rcpts, subject, body)) or "SENT"
        old_dbl = sys.modules.get("discord_bot_loop")
        sys.modules["discord_bot_loop"] = fake
        try:
            for bad in ("a@example.test,b@example.test", "a@example.test\nBcc: x@example.test", "<a@example.test>", "a@example.test;b@example.test", "", "no-at-sign"):
                try:
                    real_mailer(bad, "s", "b")
                    check("real_mailer refuses %r" % bad[:30], False)
                except RuntimeError:
                    check("real_mailer refuses %r" % bad[:30], True)
            check("refusals sent nothing", not seen)
            real_mailer("a@example.test", "Subject\r\nBcc: evil@example.test", "body")
            check("single address passed as a one-element list, subject CR/LF flattened",
                  seen and seen[0][0] == ["a@example.test"] and "\n" not in seen[0][1] and "\r" not in seen[0][1])
        finally:
            if old_dbl is None:
                del sys.modules["discord_bot_loop"]
            else:
                sys.modules["discord_bot_loop"] = old_dbl
        try:
            import inspect
            import discord_bot_loop as real_dbl
            src_send = inspect.getsource(real_dbl._outlook_send)
            check("shared _outlook_send passes subject and recipients as osascript argv, body as a file, shell=False",
                  'subprocess.run(["osascript", SEND_SCRIPT, ",".join(rcpts), subject, path]' in src_send
                  and "shell=True" not in src_send and "do shell script" not in src_send)
        except Exception as exc:
            print("  [SKIP] discord_bot_loop not importable here (%s); verified on the server run" % type(exc).__name__)
            skipped_checks.append("shared _outlook_send source check")
        print("self-test: v5 media gate (magic bytes, ffprobe, base64, size)")
        reset()
        add("m1", data=b"\x00\x00\x00\x18ftypmp42" + b"x" * 40)         # mp4 bytes declared as video/webm
        add("m2", data=b"#!/bin/sh\ntouch /tmp/oct_pwned_media\n")          # script declared as video/webm
        add("m3", data=WEBM_MAGIC + b"looks fine to the magic check")
        state["items"]["m3"]["meta"]["mime"] = "video/webm"
        c = cfg("media")
        probe["v"] = "reject"; probe["calls"].clear()
        rc = run(c, matcher=matcher, alert=fa)
        vd = os.path.dirname(c["manifest"])
        check("magic mismatch and probe rejection: exit 1, nothing emailed or transcribed, no media left in videos/",
              rc == 1 and mail["calls"] == 0 and not tcalls
              and not [f for f in os.listdir(vd) if f.endswith((".webm", ".mp4"))])
        check("probe ran only for the file that passed the magic check",
              len(probe["calls"]) == 1 and probe["calls"][0].endswith("m3.webm"))
        check("alert names the media step", len(alerts) == 1 and "media" in alerts[0][1])
        qd = os.path.join(vd, "quarantine")
        check("probe-rejected m3 is QUARANTINED (file + json without email) and deleted from Drive; m1/m2 stay",
              state["deleted"] == ["m3"] and os.path.exists(os.path.join(qd, "m3.webm"))
              and "test-address@example" not in open(os.path.join(qd, "m3.json")).read()
              and "m1" in state["items"] and "m2" in state["items"])
        check("quarantine is named in the alert", alerts and "QUARANTINED" in alerts[0][1])
        alerts.clear(); probe["calls"].clear()
        rc = run(c, matcher=matcher, alert=fa)
        check("next run: m3 gone from Drive, prober not run again", "m3" not in state["items"] and not probe["calls"])
        rc = run(c, matcher=matcher, alert=fa)
        alerts.clear()
        import io as _io, contextlib as _cl
        buf = _io.StringIO()
        with _cl.redirect_stdout(buf):
            rc = run(c, matcher=matcher, alert=fa)
        ps = json.load(open(os.path.join(vd, "pending_state.json")))
        check("deterministic failures held after %d runs, then skipped with a NOTICE and no alert" % HOLD_AFTER,
              ps.get("m1", {}).get("held") == "media" and ps.get("m2", {}).get("held") == "media"
              and rc == 0 and not alerts and "HELD" in buf.getvalue() and "m1" in buf.getvalue())
        probe["v"] = "ok"; probe["calls"].clear(); reset()
        big = "A" * (MAX_B64_CHARS + 4)
        # serve an oversized / invalid base64 field through the fake API
        for name, payload in (("oversized base64", big), ("non-base64 characters", "AAAA!!!!AAAA"), ("bad padding", "AAA")):
            reset()
            add("m4")
            state["items"]["m4"]["override_b64"] = payload
            c = cfg("b64")
            rc = run(c, matcher=matcher, alert=fa)
            check("%s: exit 1, nothing deleted or written" % name,
                  rc == 1 and state["deleted"] == [] and not read_manifest(c["manifest"]) and mail["calls"] == 0)
        reset()
        if shutil.which("ffprobe") and shutil.which("ffmpeg"):
            d = tempfile.mkdtemp(prefix="oct_media_")
            def gen(name, secs, codec):
                subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=%s" % secs, "-c:a", codec,
                                os.path.join(d, name)], check=True)
            gen("ok.webm", 2, "libopus"); gen("long.webm", 130, "libopus"); gen("ok.mp4", 2, "aac")
            open(os.path.join(d, "junk.webm"), "wb").write(WEBM_MAGIC + b"\x00" * 200)
            ok_results = []
            for nm, want_ok in (("ok.webm", True), ("ok.mp4", True), ("long.webm", False), ("junk.webm", False)):
                try:
                    transcribe_one.check_media(os.path.join(d, nm)); got_ok = True
                except transcribe_one.MediaRejected:
                    got_ok = False
                check("real ffprobe gate: %s %s" % (nm, "accepted" if want_ok else "rejected"), got_ok == want_ok)
            shutil.rmtree(d, ignore_errors=True)
        else:
            print("  [SKIP] ffmpeg/ffprobe not installed here; real media gate verified on the server run")
            skipped_checks.append("real ffprobe gate")
        print("self-test: v5 transcript scrub")
        reset()
        add("s1")
        tmode["v"] = "ok"
        _t = stub_transcriber
        c = cfg("scrub")
        c["transcriber"] = lambda path: "heard \x1b[31mred\x07 words\x00 \u2028 end"
        rc = run(c, matcher=matcher, alert=fa)
        t = open(os.path.join(os.path.dirname(c["manifest"]), "s1.transcript.txt"), encoding="utf-8").read()
        check("transcript saved without control characters", rc == 0 and t == "heard [31mred words  end" and "\x1b" not in t)

        print("self-test: v5 review: global non-registrant cap, disk guard, poison strings")
        reset()
        old_cap = globals()["NONREG_CAP_MAX"]
        globals()["NONREG_CAP_MAX"] = 2
        try:
            for n in range(4):
                add_text("g%d" % n, text="relay text %d" % n, email="victim%d-test-address@example.test" % n)
            add_text("g9", text="registrant text")
            c = cfg("globalcap")
            rc = run(c, matcher=matcher, alert=fa)
            rows = read_manifest(c["manifest"])
            nonreg_sent = [m for m in mail["sent"] if m[0].startswith("victim")]
            check("only 2 non-registrant thank-yous sent, registrant still thanked, all items pulled",
                  len(nonreg_sent) == 2 and any(m[0].startswith("registrant") for m in mail["sent"])
                  and len(state["deleted"]) == 5)
            check("the other 2 are marked abuse_cap_nonregistrant and the run fails loudly naming the cap",
                  sorted(r.get("thank_you_skipped", "") for r in rows.values()).count("abuse_cap_nonregistrant") == 2
                  and rc == 1 and alerts and "abuse_cap" in alerts[-1][1])
            capf = open(os.path.join(os.path.dirname(c["manifest"]), "thank_you_cap.json")).read()
            check("global cap state has no plaintext address", "test-address@example" not in capf and NONREG_CAP_KEY in capf)
        finally:
            globals()["NONREG_CAP_MAX"] = old_cap
        reset()
        add("h1")
        c = cfg("disk")
        c["min_free_bytes"] = 10 ** 18
        rc = run(c, matcher=matcher, alert=fa)
        check("disk guard: exit 1, nothing fetched or deleted, alert names disk",
              rc == 1 and state["deleted"] == [] and not mail["sent"] and alerts and "disk" in alerts[0][1])
        for label, field, val in (("lone surrogate in name", "name", "A\ud800B"),
                                  ("RTL override in name", "name", "Ann\u202eeeL"),
                                  ("zero-width in institution", "institution", "JHU\u200b"),
                                  ("lone surrogate in user_agent", "user_agent", "ua\udc00")):
            reset()
            add("p1")
            state["items"]["p1"]["meta"][field] = val
            rc = run(cfg("poison-" + field + str(len(val))), matcher=matcher, alert=fa)
            check("rejects %s at validate, nothing sent or deleted" % label,
                  rc == 1 and not mail["calls"] and not state["deleted"] and "validate" in alerts[0][1])

        print("self-test: missing env")
        envp = {k: v for k, v in os.environ.items() if not k.startswith("OCT_")}
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--dry-run"],
                           capture_output=True, text=True, env=dict(envp, HOME=tmp))
        check("exit 2 with FAILED banner", r.returncode == 2 and "FAILED" in r.stderr)
    finally:
        globals()["ID_RE"] = real_pat
        oct_db.ID_RE = real_oct_pat
        ROOT = old_root
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    if skipped_checks:
        print("SKIPPED CHECKS (%d, environment lacks the tool): %s" % (len(skipped_checks), ", ".join(skipped_checks)))
    print("SELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def self_test_all():
    """Pull self-test plus the oct_db and intake self-tests (all must pass)."""
    import oct_db
    import oct_corrections_intake as oci
    rcs = [self_test(), oct_db.self_test(), oci.self_test()]
    js = os.path.join(HERE, "oct_code_gs_selftest.js")
    if os.path.exists(js) and shutil.which("node"):
        r = subprocess.run(["node", js], capture_output=True, text=True)
        print("Apps Script (Code.gs) hostile-input self-test: %s (%d PASS lines)" % (
            "PASSED" if r.returncode == 0 else "FAILED", r.stdout.count("PASS ")))
        if r.returncode:
            print(r.stdout[-2000:])
        rcs.append(r.returncode)
    else:
        print("SKIPPED: Apps Script hostile-input self-test (node or %s missing here)" % js)
    print("ALL SELF-TESTS %s" % ("PASSED" if not any(rcs) else "FAILED"))
    return 1 if any(rcs) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test_all())
    setup_logging()
    cfg = load_cfg()
    rc = run(cfg, dry=a.dry_run)
    if not a.dry_run:
        rc = max(rc, run_intake_step(cfg))
    sys.exit(rc)


if __name__ == "__main__":
    main()
