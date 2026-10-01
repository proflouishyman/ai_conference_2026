#!/usr/bin/env python3
"""Server-side pull of Google Form registrant emails for the Discord gate.

Standard library only, Python 3.9 compatible. Fetches form responses with a
read-only OAuth refresh token, extracts the respondent email exactly as
sync_registrations.py does (response["respondentEmail"]), HMACs it with
GATE_PEPPER and atomically writes scripts/gate_hashes_form.json:
    {"generated_at": ..., "count": n, "hashes": [hex, ...]}
Never writes or logs an email, a name or a token; logs counts only.

Env: GATE_GOOGLE_TOKEN (default ~/.conference_google_readonly.json),
     GATE_FORM_ID (fallback: scripts/private_overrides.json "google_form_id"),
     GATE_PEPPER.
"""
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("gate_form_pull")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "gate_hashes_form.json")
COVERED = os.path.join(HERE, "gate_covered.json")   # from the laptop export
_last_ok = [0.0]


def _fp(pepper, email):
    return hmac.new(pepper.encode(), email.strip().lower().encode(),
                    hashlib.sha256).hexdigest()


def _form_id():
    fid = os.environ.get("GATE_FORM_ID", "").strip()
    if fid:
        return fid
    with open(os.path.join(HERE, "private_overrides.json")) as fh:
        return str(json.load(fh).get("google_form_id", "")).strip()


def _access_token():
    path = os.environ.get("GATE_GOOGLE_TOKEN") or os.path.expanduser(
        "~/.conference_google_readonly.json")
    with open(path) as fh:
        t = json.load(fh)
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        "client_id": t["client_id"], "client_secret": t["client_secret"],
    }).encode()
    uri = t.get("token_uri") or "https://oauth2.googleapis.com/token"
    with urllib.request.urlopen(urllib.request.Request(uri, data=data), timeout=20) as r:
        return json.load(r)["access_token"]


def _get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _covered():
    try:
        with open(COVERED) as fh:
            return set(json.load(fh).get("responses", []))
    except (OSError, ValueError):
        return None


def _hashes(pepper):
    """Emails from form responses the local DB has not seen yet. The local DB
    (with its corrections) is the base. The server only adds new registrants."""
    covered = _covered()
    if covered is None:
        raise RuntimeError("gate_covered.json missing: refusing to add uncorrected emails")
    tok = _access_token()
    base = "https://forms.googleapis.com/v1/forms/%s/responses" % urllib.parse.quote(_form_id())
    out, page = set(), None
    while True:
        q = {"pageSize": "500"}
        if page:
            q["pageToken"] = page
        resp = _get(base + "?" + urllib.parse.urlencode(q), tok)
        for r in resp.get("responses", []):
            if _fp(pepper, r.get("responseId", "")) in covered:
                continue                        # the local DB already decided this one
            e = (r.get("respondentEmail") or "").strip().lower()
            if "@" in e:
                out.add(_fp(pepper, e))
        page = resp.get("nextPageToken")
        if not page:
            return sorted(out)


def pull(min_interval_s=300):
    """Refresh gate_hashes_form.json. Never raises. Returns count or None."""
    try:
        if time.time() - _last_ok[0] < min_interval_s:
            return None
        pepper = os.environ.get("GATE_PEPPER", "")
        if not pepper:
            log.warning("form pull skipped: GATE_PEPPER not set")
            return None
        hs = _hashes(pepper)
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                       "count": len(hs), "hashes": hs}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OUT)
        _last_ok[0] = time.time()
        log.info("form pull ok: %d hashes", len(hs))
        return len(hs)
    except Exception as exc:     # type only: messages could carry URLs or ids
        log.warning("form pull failed: %s", type(exc).__name__)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = pull(0)
    print("count:", n)
    raise SystemExit(0 if n is not None else 1)
