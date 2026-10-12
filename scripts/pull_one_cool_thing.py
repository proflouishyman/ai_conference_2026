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
  {action:"fetch", id}           -> {ok:true, id, sha256, video_b64}
  {action:"delete", id, sha256}  -> {ok:true}
  any failure                    -> {ok:false, error}
Metadata json (contract): name, email, institution, consent, duration_s, mime,
user_agent, submitted_at, received_at, bytes, sha256 (+ v).

Env (from ~/.conference_bots.env): OCT_APPS_SCRIPT_URL, OCT_SERVER_SECRET.
Registrant cross-check reuses discord_gate.fp (HMAC-SHA256, GATE_PEPPER) and
discord_gate.load_hashes. Plaintext email is never written to disk.

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

log = logging.getLogger("onecoolthing")

# field -> (types, required)
META_FIELDS = {
    "name": (str, True), "email": (str, True), "institution": (str, True),
    "consent": (bool, True), "duration_s": ((int, float), True),
    "mime": (str, True), "user_agent": (str, False), "submitted_at": (str, True),
    "received_at": (str, True), "bytes": (int, True), "sha256": (str, True),
    "v": (int, False),
}


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
def validate_meta(meta, item_id):
    if not isinstance(meta, dict):
        raise Fail("validate", "metadata for %s is not an object" % item_id)
    for k, (typ, req) in META_FIELDS.items():
        if k not in meta:
            if req:
                raise Fail("validate", "metadata missing field %r" % k)
            continue
        v = meta[k]
        if isinstance(v, bool) and typ is not bool:
            raise Fail("validate", "field %r is bool, expected %s" % (k, typ))
        if not isinstance(v, typ):
            raise Fail("validate", "field %r has type %s" % (k, type(v).__name__))
    if meta["consent"] is not True:
        raise Fail("validate", "consent is not true")
    if meta["mime"] not in EXT:
        raise Fail("validate", "mime %r not allowed" % meta["mime"])
    if len(meta["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in meta["sha256"]):
        raise Fail("validate", "metadata sha256 malformed")
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


# ------------------------------------------------------------ one item
def process_item(cfg, item, manifest, matcher, vdir, counts):
    item_id = safe_id(item.get("id") if isinstance(item, dict) else None)
    meta = item.get("json")
    validate_meta(meta, item_id)
    want = meta["sha256"]

    if item_id in manifest:
        row = manifest[item_id]
        path = os.path.join(ROOT, row["path"]) if not os.path.isabs(row["path"]) else row["path"]
        if not os.path.exists(path):
            raise Fail("recover", "in manifest but file missing: %s" % path)
        with open(path, "rb") as fh:
            if sha_hex(fh.read()) != row["sha256"] or row["sha256"] != want:
                raise Fail("recover", "in manifest but local file sha256 differs; NOT deleting")
        api(cfg, "delete", id=item_id, sha256=want)
        counts["had"] += 1
        log.info("%s already pulled; delete retried OK", item_id)
        return

    res = api(cfg, "fetch", id=item_id)
    api_sha = res.get("sha256")
    b64 = res.get("video_b64")
    if not isinstance(b64, str) or not isinstance(api_sha, str):
        raise Fail("fetch", "response lacks video_b64/sha256 strings")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise Fail("decode", str(exc)[:200])
    got = sha_hex(data)
    if not (got == api_sha == want):
        raise Fail("verify", "sha256 mismatch computed=%s api=%s meta=%s" % (got, api_sha, want))
    if len(data) != meta["bytes"]:
        raise Fail("verify", "bytes mismatch got=%d meta=%d" % (len(data), meta["bytes"]))

    ext = EXT[meta["mime"]]
    vpath = os.path.join(vdir, "%s.%s" % (item_id, ext))
    local_meta = {k: v for k, v in meta.items() if k != "email"}
    local_meta["email_hash_match"] = bool(matcher(meta["email"]))
    atomic_write(vpath, data)
    atomic_write(os.path.join(vdir, item_id + ".json"),
                 json.dumps(local_meta, indent=1, sort_keys=True).encode())
    row = {
        "id": item_id, "name": meta["name"], "email_hash_match": local_meta["email_hash_match"],
        "institution": meta["institution"], "consent": meta["consent"],
        "duration_s": meta["duration_s"], "mime": meta["mime"], "bytes": len(data),
        "sha256": got, "submitted_at": meta["submitted_at"],
        "received_at": meta["received_at"], "pulled_at": now_iso(),
        "path": os.path.relpath(vpath, ROOT), "source": SOURCE,
    }
    append_manifest(cfg["manifest"], row)
    manifest[item_id] = row
    counts["pulled"] += 1
    counts["matches"] += 1 if row["email_hash_match"] else 0
    log.info("%s pulled (%d bytes, registrant=%s)", item_id, len(data), row["email_hash_match"])
    api(cfg, "delete", id=item_id, sha256=got)    # only after local write + manifest
    log.info("%s deleted from Drive", item_id)


# ------------------------------------------------------------------ main run
def run(cfg, dry=False, matcher=None, alert=None):
    counts = dict(pulled=0, had=0, failed=0, matches=0)
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
    return {"url": os.environ["OCT_APPS_SCRIPT_URL"], "secret": os.environ["OCT_SERVER_SECRET"],
            "manifest": os.path.join(ROOT, "videos", "manifest.jsonl")}


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

    tmp = tempfile.mkdtemp(prefix="oct_selftest_")
    alerts = []
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        ok = ok and cond

    def cfg(sub):
        d = os.path.join(tmp, sub)
        return {"url": "http://127.0.0.1:%d/" % srv.server_port, "secret": SECRET,
                "manifest": os.path.join(d, "videos", "manifest.jsonl"), "retry_sleep": 0}

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
        txt = open(c["manifest"]).read() + open(os.path.join(vd, "a1.json")).read()
        check("no plaintext email on disk", "test-address" not in txt)
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
