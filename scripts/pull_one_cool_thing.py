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

Exit codes: 0 ok, 1 one or more failures (alert emailed), 2 config missing.
Loud failure: no fallbacks, no coercion. Python 3.9 compatible.

  --dry-run    list only, no fetch/delete/writes
  --self-test  run against an in-process fake API
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import subprocess
import tempfile
import time
import urllib.request
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
CAP_MAX = 3
CAP_WINDOW_S = 24 * 3600
THANKS_SUBJECT = "Thank you for sharing one cool thing"

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
                raw = r.read()
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


def safe_id(item_id):
    if not isinstance(item_id, str) or not item_id or not all(
            c.isalnum() or c in "-_" for c in item_id):
        raise Fail("validate", "unsafe or empty id %r" % (item_id,))
    return item_id


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
    cmd = [python or WHISPER_PY, helper or TRANSCRIBE_HELPER, media_path]
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
    import discord_bot_loop as dbl
    return dbl._outlook_send([to], subject, body)


def thank_you_body(mode, name, text):
    if mode == "text":
        return ("Dear %s,\n\nThank you for sharing one cool thing from the AI and History Conference. "
                "Here is what you wrote:\n\n\"%s\"\n\n"
                "If anything looks wrong, or you would like your submission removed, "
                "email lhyman6@jh.edu.\n" % (name, text))
    extra = ""
    if mode == "transcript_only":
        extra = "As you asked, we will only use your words, never the video itself.\n\n"
    return ("Dear %s,\n\nThank you for sharing one cool thing from the AI and History Conference.\n\n"
            "Here is the transcript of your recording:\n\n\"%s\"\n\n"
            "The transcript was made by a private, local LLM running on our own server at Johns Hopkins. "
            "Your recording was not sent to any cloud service.\n\n%s"
            "If anything in the transcript looks wrong, or you would like your submission removed, "
            "email lhyman6@jh.edu.\n" % (name, text, extra))


def thank_you(cfg, vdir, item_id, meta, mode, text, pend, pstate, counts):
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
    try:
        outcome = cfg["mailer"](meta["email"], THANKS_SUBJECT, thank_you_body(mode, meta["name"], text))
    except Exception as exc:
        log.error("EMAIL FAILED thank-you %s: %s", item_id, str(exc)[:200])
        raise Fail("email", "send failed, nothing sent: %s" % str(exc)[:200])
    if outcome == "SENT":
        pend["thank_you_sent"] = True
        pstate[item_id] = pend
        save_state(os.path.join(vdir, "pending_state.json"), pstate)   # before any delete
        cap[key] = recent + [now]
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
        if not os.path.exists(path):
            raise Fail("recover", "in manifest but file missing: %s" % path)
        with open(path, "rb") as fh:
            if sha_hex(fh.read()) != row["sha256"] or row["sha256"] != want:
                raise Fail("recover", "in manifest but local file sha256 differs; NOT deleting")
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
        tpath = os.path.join(vdir, item_id + ".txt")
        atomic_write(tpath, data)
        atomic_write(os.path.join(vdir, item_id + ".json"),
                     json.dumps(local_meta, indent=1, sort_keys=True).encode())
        row.update({"duration_s": None, "mime": None, "bytes": 0, "sha256": got,
                    "path": None, "has_video": False, "text_chars": len(text)})
        media_path, mail_text = None, text
    else:
        b64 = res.get("video_b64")
        if not isinstance(b64, str):
            raise Fail("fetch", "response lacks video_b64 string")
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise Fail("decode", str(exc)[:200])
        got = sha_hex(data)
        if not (got == api_sha == want):
            raise Fail("verify", "sha256 mismatch computed=%s api=%s meta=%s" % (got, api_sha, want))
        if len(data) != meta["bytes"]:
            raise Fail("verify", "bytes mismatch got=%d meta=%d" % (len(data), meta["bytes"]))
        vpath = os.path.join(vdir, "%s.%s" % (item_id, EXT[meta["mime"]]))
        atomic_write(vpath, data)
        atomic_write(os.path.join(vdir, item_id + ".json"),
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
        tpath = os.path.join(vdir, item_id + ".transcript.txt")
        if pend.get("transcribed") is True and os.path.exists(tpath):
            with open(tpath, encoding="utf-8") as fh:
                mail_text = fh.read()
            if len(mail_text) != pend.get("transcript_chars") or not mail_text.strip():
                raise Fail("transcribe", "saved transcript for %s does not match pending_state" % item_id)
        else:
            mail_text = cfg["transcriber"](media_path)
            if not isinstance(mail_text, str) or not mail_text.strip():
                raise Fail("transcribe", "empty transcript")
            mail_text = mail_text.strip()
            atomic_write(tpath, mail_text.encode("utf-8"))
            pend.update({"transcribed": True, "transcript_chars": len(mail_text)})
            pstate[item_id] = pend
            save_state(pend_path, pstate)
        row["transcript_chars"] = len(mail_text)
    else:
        row["transcript_chars"] = 0
    row["thank_you_sent"] = thank_you(cfg, vdir, item_id, meta, mode, mail_text, pend, pstate, counts)
    if not row["thank_you_sent"]:
        row["thank_you_skipped"] = pend.get("thank_you_skipped", "abuse_cap")
    row["pulled_at"] = now_iso()
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
    counts = dict(pulled=0, had=0, failed=0, matches=0, cap_skipped=[])
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
            len(items), ", ".join(str(i.get("id")) for i in items if isinstance(i, dict))))
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
    for item in items:
        iid = item.get("id") if isinstance(item, dict) else None
        try:
            process_item(cfg, item, manifest, matcher, vdir, counts)
        except Fail as f:
            failures.append((iid, f.step, f.msg))
            counts["failed"] += 1
            log.error("FAILED %s step=%s: %s", iid, f.step, f.msg)
        except Exception as exc:
            failures.append((iid, "unexpected", "%s: %s" % (type(exc).__name__, exc)))
            counts["failed"] += 1
            log.exception("FAILED %s unexpected", iid)
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
                "Nothing was deleted from Drive for failed items.\n\n%s\n\nLog: %s\n"
                % (len(failures), "\n".join(lines), LOG_PATH))
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
            "fp": lambda email: dg.fp(pepper, email)}


# ------------------------------------------------------------------ self-test
def self_test():
    import http.server
    import shutil
    import subprocess
    import threading

    state = {"items": {}, "deleted": [], "bad_delete": False}
    SECRET = "s3cret"

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
                           "video_b64": base64.b64encode(v["data"]).decode()}
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

    def add(i, data=b"fakevideo-" + os.urandom(8), api_sha=None, email="registrant-test-address"):
        h = sha_hex(data)
        state["items"][i] = {"data": data, "api_sha": api_sha or h, "meta": {
            "v": 1, "name": "T " + i, "email": email, "institution": "X", "consent": True,
            "duration_s": 30, "mime": "video/webm", "user_agent": "ua",
            "submitted_at": "2026-10-12T00:00:00Z", "received_at": "2026-10-12T00:00:01Z",
            "bytes": len(data), "sha256": h}}

    def add_text(i, text="Un historien tapé \u00e9\u2603 text", api_sha=None, extra=None,
                 email="registrant-test-address"):
        data = text.encode("utf-8")
        h = sha_hex(data)
        meta = {"v": 2, "mode": "text", "name": "T " + i, "email": email, "institution": "X",
                "consent": True, "consent_text": "I agree (text)", "text": text,
                "user_agent": "ua", "submitted_at": "2026-10-12T00:00:00Z",
                "received_at": "2026-10-12T00:00:01Z", "bytes": 0, "sha256": h}
        meta.update(extra or {})
        state["items"][i] = {"data": data, "api_sha": api_sha or h, "meta": meta}

    def add_tr(i):
        add(i)
        state["items"][i]["meta"].update({"v": 2, "mode": "transcript_only",
                                          "consent_text": "I agree (transcript)"})

    tmp = tempfile.mkdtemp(prefix="oct_selftest_")
    alerts = []
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        ok = ok and cond

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
                "transcriber": stub_transcriber, "mailer": stub_mailer,
                "fp": lambda e: "fp-" + sha_hex(e.encode())[:16]}

    def reset():
        state["items"].clear(); state["deleted"].clear(); alerts.clear()
        mail["sent"].clear(); mail["mode"] = "ok"; mail["calls"] = 0
        tcalls.clear(); tmode["v"] = "ok"

    matcher = lambda e: e == "registrant-test-address"
    fa = lambda s, b: alerts.append((s, b))
    global ROOT
    old_root, ROOT = ROOT, tmp
    try:
        print("self-test: happy path")
        add("a1")
        add("a2", email="stranger-test-address")
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
              "test-address" not in alltxt)
        check("thank-yous sent to both, rows thank_you_sent true with transcript_chars",
              len(mail["sent"]) == 2 and rows["a1"]["thank_you_sent"] is True
              and rows["a1"]["transcript_chars"] == len(open(os.path.join(vd, "a1.transcript.txt")).read()) > 0)
        check("pending_state empty after success",
              json.load(open(os.path.join(vd, "pending_state.json"))) == {})
        check("video file written", os.path.getsize(os.path.join(vd, "a1.webm")) > 0)
        check("no alert", not alerts)

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
              and r.get("consent_text") == "I agree (text)" and r.get("email_hash_match") is True)
        check("txt file holds the text exactly",
              open(os.path.join(vd, "t1.txt"), "rb").read() == "Un historien tapé \u00e9\u2603 text".encode("utf-8"))
        jtxt = open(os.path.join(vd, "t1.json")).read()
        check("json has no email, has email_hash_match", "test-address" not in jtxt
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
              and r.get("consent_text") == "I agree (transcript)")
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
              and "never the video itself" in body and mail["sent"][0][1] == THANKS_SUBJECT)
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
            add("k%d" % n, email="cap-test-address")
        add("k9", email="other-test-address")
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
        check("cap file keyed by fp, no plaintext email", "fp-" in capf and "test-address" not in capf)

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

        print("self-test: missing env")
        envp = {k: v for k, v in os.environ.items() if not k.startswith("OCT_")}
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--dry-run"],
                           capture_output=True, text=True, env=dict(envp, HOME=tmp))
        check("exit 2 with FAILED banner", r.returncode == 2 and "FAILED" in r.stderr)
    finally:
        ROOT = old_root
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    setup_logging()
    cfg = load_cfg()
    sys.exit(run(cfg, dry=a.dry_run))


if __name__ == "__main__":
    main()
