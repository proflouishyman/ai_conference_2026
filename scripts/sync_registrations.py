#!/usr/bin/env python3
"""Sync AI and History Conference 2026 registrations from the Google Form
into a local SQLite DB (registrations.db, gitignored — never pushed).

Usage:
    python3 sync_registrations.py

Reads all responses from the live form each run (the API has no incremental
mode we're using here) and upserts by responseId, so re-runs are safe and
new registrants just get added on top of what's already stored.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

FORM_ID = "1JFcQssF_JWfrVM28yOP-Wh65j5t2nC57RrdjByNCj8o"
FORMS_SKILL_DIR = Path.home() / ".claude/skills/google-forms"
DB_PATH = Path(__file__).resolve().parent.parent / "registrations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    response_id      TEXT PRIMARY KEY,
    create_time      TEXT NOT NULL,
    email            TEXT,
    first_name       TEXT,
    last_name        TEXT,
    institution      TEXT,
    role              TEXT,
    department       TEXT,
    fee_category     TEXT,
    experience_level TEXT,
    primary_area     TEXT,
    heard_via        TEXT,
    raw_answers_json TEXT NOT NULL,
    synced_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_registrations_email ON registrations(email);
CREATE INDEX IF NOT EXISTS idx_registrations_fee ON registrations(fee_category);
"""


def fetch_responses():
    result = subprocess.run(
        [str(FORMS_SKILL_DIR / ".venv/bin/python"),
         str(FORMS_SKILL_DIR / "scripts/get_responses.py"), FORM_ID],
        capture_output=True, text=True, cwd=str(FORMS_SKILL_DIR),
    )
    if result.returncode != 0:
        sys.exit(f"get_responses.py failed: {result.stderr}")
    out = result.stdout
    start = out.find("[")
    if start == -1:
        sys.exit(f"No JSON array found in output: {out[:500]}")
    return json.loads(out[start:])


def main():
    responses = fetch_responses()

    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)

    inserted = 0
    updated = 0
    for r in responses:
        a = r.get("answers", {})
        response_id = r["responseId"]
        existing = conn.execute(
            "SELECT 1 FROM registrations WHERE response_id = ?", (response_id,)
        ).fetchone()

        conn.execute(
            """
            INSERT INTO registrations (
                response_id, create_time, email, first_name, last_name,
                institution, role, department, fee_category,
                experience_level, primary_area, heard_via, raw_answers_json,
                synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(response_id) DO UPDATE SET
                create_time=excluded.create_time,
                email=excluded.email,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                institution=excluded.institution,
                role=excluded.role,
                department=excluded.department,
                fee_category=excluded.fee_category,
                experience_level=excluded.experience_level,
                primary_area=excluded.primary_area,
                heard_via=excluded.heard_via,
                raw_answers_json=excluded.raw_answers_json,
                synced_at=datetime('now')
            """,
            (
                response_id,
                r.get("createTime", ""),
                r.get("respondentEmail", ""),
                a.get("First Name", ""),
                a.get("Last Name", ""),
                a.get("Institution / Affiliation", ""),
                a.get("Position / Role", ""),
                a.get("Department / Field", ""),
                a.get("Registration fee category", ""),
                a.get("2. How would you describe your experience with computational or AI methods in historical research?", ""),
                a.get("4. What is your primary research area or period?", ""),
                a.get("How did you hear about this conference?", ""),
                json.dumps(a, ensure_ascii=False),
            ),
        )
        if existing:
            updated += 1
        else:
            inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
    conn.close()

    print(f"Synced: {inserted} new, {updated} updated, {total} total rows in {DB_PATH}")


if __name__ == "__main__":
    main()
