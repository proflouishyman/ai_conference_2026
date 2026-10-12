#!/usr/bin/env python3
"""One Cool Thing mail sender (v7).

Same hand-off as discord_bot_loop._outlook_send (the shared Discord-bot file, which we do not
edit): the outlook-correspondence send_mail.applescript run through osascript with an ARGV LIST
(recipients, subject, body temp file; no shell), from the account pinned by the lock file, with the
ces signature appended. Only a confirmed SENT counts as success. UNCONFIRMED (timeout, or no SENT
line) is returned, never retried. ABORTED / bad config / osascript will not start raise RuntimeError.

What differs: the shared path writes the recipient address AND the first 500 chars of the body into
~/coding/email/db/mail-index.db (send_log) on this machine. For One Cool Thing that would put every
submitter's plaintext address and transcript into a second database, so here the send is recorded
with the literal placeholder recipient "submitter" and a body note that carries no transcript.
Untagged alerts to Louis are recorded under his own (public) address with the same body note; anything
whose subject carries "[OCT <id>]" is always "submitter".
The send is still recorded (not skipped) so the mail index can see that a send was attempted and
whether it was confirmed. The sent message itself still sits in the ces-agora Outlook Sent Items;
that is the mailbox, not something this module can avoid.

Contract
  send(rcpts: list[str], subject: str, body: str) -> "SENT" | "UNCONFIRMED"   (else RuntimeError)
  env: BOT_MAIL_FROM must equal the lock file's pinned account (loaded by pull's load_cfg).
Self-test: python3 oct_mail.py --self-test   (osascript and record-send are stubbed)
"""
import logging
import os
import subprocess
import sys
import tempfile

log = logging.getLogger("onecoolthing")

SEND_SCRIPT = os.path.expanduser("~/.claude/skills/outlook-correspondence/scripts/send_mail.applescript")
LOCK_FILE = os.path.expanduser("~/.config/outlook-correspondence/only_account")
SIGNATURE = os.path.expanduser("~/coding/email/signature_ces.txt")
MAIL_INDEX = os.path.expanduser("~/coding/email/scripts/mail_index.py")
SEND_TIMEOUT_S = 200
LOUIS = "lhyman6@jh.edu"
PLACEHOLDER_TO = "submitter"
REDACTED_BODY = "[redacted: One Cool Thing]"


def log_recipient(rcpts, subject):
    """What the mail index may see. Anything tagged "[OCT <id>]" (thank-yous and correction replies, i.e.
    mail about one submission) is always the literal placeholder, even when the submitter is Louis. Only
    untagged operational alerts to Louis are logged under his own (public) address."""
    if "[OCT " in subject:
        return PLACEHOLDER_TO
    return LOUIS if rcpts and all(r.strip().lower() == LOUIS for r in rcpts) else PLACEHOLDER_TO


def send(rcpts, subject, body, runner=None):
    """Send one message. Returns 'SENT' or 'UNCONFIRMED'; raises RuntimeError when nothing was sent."""
    run = runner or subprocess.run
    sender = os.environ.get("BOT_MAIL_FROM", "").strip().lower()
    if not sender:
        raise RuntimeError("BOT_MAIL_FROM is not set; nothing sent")
    try:
        lock = open(LOCK_FILE).read().strip().lower()
        sig = open(SIGNATURE).read().strip()
    except OSError as exc:
        raise RuntimeError("cannot read %s; nothing sent" % exc.filename)
    if lock != sender:
        raise RuntimeError("lock file pins %r, BOT_MAIL_FROM is %r; nothing sent" % (lock, sender))
    signed = body.rstrip() + "\n\nBest,\n\n" + sig + "\n"
    fd, path = tempfile.mkstemp(prefix="octmail_", suffix=".txt")
    fd2, note_path = tempfile.mkstemp(prefix="octmail_note_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(signed)
        with os.fdopen(fd2, "w", encoding="utf-8") as fh:
            fh.write(REDACTED_BODY + "\n")
        env = dict(os.environ, OUTLOOK_FROM=sender)
        try:
            r = run(["osascript", SEND_SCRIPT, ",".join(rcpts), subject, path],
                    capture_output=True, text=True, timeout=SEND_TIMEOUT_S, env=env)
        except subprocess.TimeoutExpired:
            outcome, detail = "UNCONFIRMED", "osascript timed out"
        except OSError as exc:
            raise RuntimeError("osascript would not start (%s); nothing sent" % exc)
        else:
            lines = (r.stdout or "").strip().splitlines()
            last = lines[-1] if lines else ""
            if last.startswith("SENT"):
                outcome, detail = "SENT", ""
            elif last.startswith("ABORTED"):
                raise RuntimeError(last[:200])
            else:
                outcome = "UNCONFIRMED"
                detail = (last or (r.stderr or "").strip() or "rc=%s" % r.returncode)[:200]
        try:
            rec = run([sys.executable, MAIL_INDEX, "record-send", "--outcome", outcome,
                       "--to", log_recipient(rcpts, subject), "--subject", subject,
                       "--body-file", note_path, "--script", "send_mail",
                       "--note", "one cool thing", "--no-sync"],
                      capture_output=True, text=True, timeout=120)
            if rec.returncode != 0 or '"logged"' not in (rec.stdout or ""):
                log.error("RECORD-SEND FAILED after %s (%s): %s", outcome, subject[:80],
                          ((rec.stdout or "") + (rec.stderr or "")).strip()[:200])
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.error("RECORD-SEND FAILED after %s (%s): %s", outcome, subject[:80], type(exc).__name__)
        if outcome == "UNCONFIRMED":
            log.error("EMAIL UNCONFIRMED (%s): %s. May still be queued in the ces-agora Outbox. Not retried.",
                      subject[:80], detail)
        return outcome
    finally:
        for p in (path, note_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def self_test():
    import shutil
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        ok = ok and bool(cond)

    class R:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    tmp = tempfile.mkdtemp(prefix="octmail_test_")
    global LOCK_FILE, SIGNATURE
    old = (LOCK_FILE, SIGNATURE, os.environ.get("BOT_MAIL_FROM"))
    try:
        LOCK_FILE, SIGNATURE = os.path.join(tmp, "lock"), os.path.join(tmp, "sig")
        open(LOCK_FILE, "w").write("bot@example.test\n")
        open(SIGNATURE, "w").write("The Signature\n")
        os.environ["BOT_MAIL_FROM"] = "bot@example.test"
        ADDR = "person-one@example.test"
        BODY = "Your transcript: I loved the archive session. SECRETWORDS"

        def make(osa_out, osa_exc=None, rec_rc=0, rec_out='{"logged": 7}'):
            calls = []

            def runner(cmd, **kw):
                f = cmd[cmd.index("--body-file") + 1] if "--body-file" in cmd else cmd[-1]
                calls.append((cmd, kw, open(f).read() if os.path.exists(f) else None))
                if cmd[0] == "osascript":
                    if osa_exc:
                        raise osa_exc
                    return R(0, osa_out)
                return R(rec_rc, rec_out)
            return runner, calls

        runner, calls = make("working\nSENT\n")
        out = send([ADDR], "Thank you [OCT x]", BODY, runner=runner)
        osa = [c for c in calls if c[0][0] == "osascript"]
        rec = [c for c in calls if c[0][0] != "osascript"]
        check("SENT returned; exactly one osascript call, argv list, no shell", out == "SENT" and len(osa) == 1
              and isinstance(osa[0][0], list) and "shell" not in osa[0][1])
        check("osascript argv is script, recipients, subject, body file; env pins the account",
              osa[0][0][1:4] == [SEND_SCRIPT, ADDR, "Thank you [OCT x]"] and osa[0][1]["env"]["OUTLOOK_FROM"] == "bot@example.test")
        check("message body is the signed text", osa[0][2].startswith(BODY) and "The Signature" in osa[0][2])
        rc = rec[0][0]
        check("record-send gets the placeholder recipient, never the address",
              rc[rc.index("--to") + 1] == "submitter" and ADDR not in " ".join(rc))
        check("record-send body file is the redaction note, no transcript",
              rec[0][2].strip() == REDACTED_BODY and "SECRETWORDS" not in rec[0][2] and "--no-sync" in rc)
        runner, calls = make("SENT")
        send([LOUIS], "alert", BODY, runner=runner)
        rc = [c for c in calls if c[0][0] != "osascript"][0][0]
        check("untagged alert to Louis is logged under his own address, still without the body",
              rc[rc.index("--to") + 1] == LOUIS)
        runner, calls = make("SENT")
        send([LOUIS], "Thank you [OCT 20261012-000000-abcdef]", BODY, runner=runner)
        rc = [c for c in calls if c[0][0] != "osascript"][0][0]
        check("a tagged thank-you is 'submitter' even when the submitter is Louis",
              rc[rc.index("--to") + 1] == "submitter")
        runner, calls = make("")
        check("no SENT line = UNCONFIRMED, one send attempt only, still recorded",
              send([ADDR], "s", "b", runner=runner) == "UNCONFIRMED"
              and sum(1 for c in calls if c[0][0] == "osascript") == 1 and len(calls) == 2
              and calls[1][0][calls[1][0].index("--outcome") + 1] == "UNCONFIRMED")
        runner, calls = make("", osa_exc=subprocess.TimeoutExpired("osascript", 1))
        check("timeout = UNCONFIRMED, not retried", send([ADDR], "s", "b", runner=runner) == "UNCONFIRMED"
              and sum(1 for c in calls if c[0][0] == "osascript") == 1)
        runner, calls = make("ABORTED: no such account")
        try:
            send([ADDR], "s", "b", runner=runner)
            check("ABORTED raises and records nothing", False)
        except RuntimeError:
            check("ABORTED raises and records nothing", len(calls) == 1)
        runner, calls = make("", osa_exc=OSError("nope"))
        try:
            send([ADDR], "s", "b", runner=runner)
            check("osascript cannot start raises", False)
        except RuntimeError:
            check("osascript cannot start raises", True)
        import logging as _l
        records = []

        class H(_l.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = H()
        log.addHandler(h)
        runner, calls = make("SENT", rec_rc=1, rec_out="boom")
        res = send([ADDR], "s", "b", runner=runner)
        log.removeHandler(h)
        check("record-send failure is logged loudly but does not turn a SENT into a failure",
              res == "SENT" and any("RECORD-SEND FAILED" in m for m in records) and not any(ADDR in m for m in records))
        runner, calls = make("SENT")
        send([ADDR], "s", "b", runner=runner)
        used = [calls[0][0][-1]] + [c[0][c[0].index("--body-file") + 1] for c in calls[1:]]
        check("both temp files are deleted afterwards", not any(os.path.exists(u) for u in used))
        os.environ["BOT_MAIL_FROM"] = "other@example.test"
        try:
            send([ADDR], "s", "b", runner=make("SENT")[0])
            check("lock/sender mismatch raises before sending", False)
        except RuntimeError:
            check("lock/sender mismatch raises before sending", True)
        os.environ.pop("BOT_MAIL_FROM")
        try:
            send([ADDR], "s", "b", runner=make("SENT")[0])
            check("missing BOT_MAIL_FROM raises", False)
        except RuntimeError:
            check("missing BOT_MAIL_FROM raises", True)
        src = open(os.path.abspath(__file__), encoding="utf-8").read().split("def self_test")[0]
        check("no shell=True / os.system in the sender", "shell=True" not in src and "os.system" not in src)
    finally:
        LOCK_FILE, SIGNATURE = old[0], old[1]
        if old[2] is None:
            os.environ.pop("BOT_MAIL_FROM", None)
        else:
            os.environ["BOT_MAIL_FROM"] = old[2]
        shutil.rmtree(tmp, ignore_errors=True)
    print("OCT_MAIL SELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    print("library module; use --self-test")
    sys.exit(2)
