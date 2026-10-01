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
    # alternates belong to a panelist, so they carry the speaker flag too
    for (e,) in c.execute("SELECT alt_email FROM alternate_emails"):
        add(e, True)
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
    if created:
        print("NEW PEPPER written to %s (GATE_PEPPER). Copy that line to the "
              "server's ~/.conference_bots.env BEFORE the gate runs there." % ENV)
    if "--no-scp" in sys.argv:
        return 0
    try:
        r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=10", "-o",
                            "BatchMode=yes", str(OUT), REMOTE],
                           timeout=20, capture_output=True)
        print("scp to ces-server:", "ok" if r.returncode == 0 else "failed (best effort)")
    except Exception as exc:
        print("scp to ces-server skipped:", type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
