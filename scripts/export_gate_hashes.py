#!/usr/bin/env python3
"""Export HMAC fingerprints of registered emails for the Discord gate.

Writes scripts/gate_hashes.json: {hmac_hex: {"speaker": bool}}. Plain emails
never leave this laptop. Pepper = env GATE_PEPPER, else generated once into
~/.conference_bots.env (copy that line to the server's ~/.conference_bots.env).
Then scp to ces-server (best effort, 20 s timeout). Python 3.9 compatible.
"""
import hmac, hashlib, json, os, secrets, sqlite3, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "registrations.db"
OUT = HERE / "gate_hashes.json"
ENV = Path.home() / ".conference_bots.env"
REMOTE = "ces-server:coding/ai_conference_2026/scripts/gate_hashes.json"
# Every form response the local DB has seen (including cancelled and excluded
# ones). The server adds a form email only if its response is NOT in here, so
# local corrections, cancellations and dedupes always win.
COVERED = HERE / "gate_covered.json"
REMOTE_COVERED = "ces-server:coding/ai_conference_2026/scripts/gate_covered.json"


def fp(pepper, email):
    return hmac.new(pepper.encode(), email.strip().lower().encode(),
                    hashlib.sha256).hexdigest()


def get_pepper():
    if os.environ.get("GATE_PEPPER"):
        return os.environ["GATE_PEPPER"], False
    try:
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("GATE_PEPPER="):
                v = line.partition("=")[2].strip().strip("'\"")
                if v:
                    return v, False
    except OSError:
        pass
    p = secrets.token_hex(32)
    with open(ENV, "a") as fh:
        fh.write("\nexport GATE_PEPPER=%s\n" % p)
    return p, True


def build(db_path, pepper):
    c = sqlite3.connect(str(db_path))
    out = {}

    def add(email, speaker):
        e = (email or "").strip().lower()
        if "@" not in e:
            return
        h = fp(pepper, e)
        out[h] = {"speaker": bool(out.get(h, {}).get("speaker") or speaker)}

    for (e,) in c.execute("SELECT email FROM registrations_corrected "
                          "WHERE coalesce(attend_type,'') NOT LIKE '%CANCEL%'"):
        add(e, False)
    for (e,) in c.execute("SELECT email FROM panelists"):
        add(e, True)
    # an alternate carries the speaker flag only when its canonical address is a panelist's
    # (registrants can have alternates too, e.g. a @jh.edu alias of a @jhu.edu registration)
    for (e, is_pan) in c.execute(
            "SELECT a.alt_email, EXISTS(SELECT 1 FROM panelists p "
            "WHERE lower(trim(p.email)) = lower(trim(a.canonical_email))) "
            "FROM alternate_emails a"):
        add(e, bool(is_pan))
    return out


def main():
    pepper, created = get_pepper()
    out = build(DB, pepper)
    tmp = str(OUT) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh, sort_keys=True)
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o600)
    print("gate_hashes.json: %d hashes, %d speakers" %
          (len(out), sum(1 for v in out.values() if v["speaker"])))
    c = sqlite3.connect(str(DB))
    covered = sorted(fp(pepper, rid) for (rid,) in c.execute("SELECT response_id FROM registrations"))
    tmp = str(COVERED) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"responses": covered}, fh)
    os.replace(tmp, COVERED)
    os.chmod(COVERED, 0o600)
    print("gate_covered.json: %d form responses already in the local DB" % len(covered))
    if created:
        print("NEW PEPPER written to %s (GATE_PEPPER). Copy that line to the "
              "server's ~/.conference_bots.env BEFORE the gate runs there." % ENV)
    if "--no-scp" in sys.argv:
        return 0
    # A failed push leaves the Discord gate on stale data, so it fails loudly (exit 2).
    failed = []
    for src, dst in ((COVERED, REMOTE_COVERED), (OUT, REMOTE)):   # covered first
        try:
            r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=10", "-o",
                                "BatchMode=yes", str(src), dst],
                               timeout=20, capture_output=True, text=True)
            err = r.stderr.strip() if r.returncode else ""
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
        print("scp %s to ces-server:" % src.name, "FAILED" if err else "ok")
        if err:
            failed.append((src.name, err))
    if failed:
        print("\n" + "=" * 70 + "\nFAILED: gate files NOT pushed to ces-server. The Discord gate is"
              "\nusing stale data. ces-server resolves only on the JHU network or VPN."
              "\nConnect, then rerun: python3 scripts/export_gate_hashes.py")
        for name, err in failed:
            print("  %s: %s" % (name, err))
        print("=" * 70)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
