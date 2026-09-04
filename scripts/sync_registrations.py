#!/usr/bin/env python3
"""Sync AI and History Conference 2026 registrations from the Google Form
into a local SQLite DB (registrations.db, gitignored — never pushed).

Usage:
    python3 sync_registrations.py

Reads all responses from the live form each run (the API has no incremental
mode we're using here) and upserts by responseId, so re-runs are safe and
new registrants just get added on top of what's already stored.

HAND-MAINTAINED SIDE TABLES -- do not fold these into `registrations`.
`registrations` is rebuilt from the form on every run, so anything written
directly into it can be overwritten by the next sync. Facts that come from
outside the form therefore live in their own tables, which this script
creates with CREATE TABLE IF NOT EXISTS and never drops or rewrites:

  corrections        -- per-field overrides (e.g. a bad email on a response)
  excluded_duplicates-- responses to hide from the corrected view
  panelists          -- who is presenting, which sessions, confirmed/pending
  alternate_emails   -- other addresses a panelist uses, mapped to canonical

Views that read them:
  registrations_corrected      -- registrations minus dupes, with corrections
  panelist_registration_status -- has each panelist registered, under EITHER
                                  their canonical or an alternate address
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
    attend_type      TEXT,
    experience_level TEXT,
    tools_used       TEXT,
    primary_area     TEXT,
    topic_wanted     TEXT,
    question_for_conference TEXT,
    dietary_restrictions TEXT,
    accessibility_needs  TEXT,
    heard_via        TEXT,
    raw_answers_json TEXT NOT NULL,
    synced_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_registrations_email ON registrations(email);
CREATE INDEX IF NOT EXISTS idx_registrations_fee ON registrations(fee_category);
CREATE INDEX IF NOT EXISTS idx_registrations_attend_type ON registrations(attend_type);

CREATE TABLE IF NOT EXISTS corrections (
    response_id     TEXT NOT NULL,
    field           TEXT NOT NULL,
    corrected_value TEXT NOT NULL,
    reason          TEXT,
    corrected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (response_id, field),
    FOREIGN KEY (response_id) REFERENCES registrations(response_id)
);

CREATE TABLE IF NOT EXISTS excluded_duplicates (
    response_id  TEXT PRIMARY KEY,
    duplicate_of TEXT NOT NULL,
    reason       TEXT,
    excluded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (response_id) REFERENCES registrations(response_id),
    FOREIGN KEY (duplicate_of) REFERENCES registrations(response_id)
);

CREATE TABLE IF NOT EXISTS panelists (
    email          TEXT PRIMARY KEY,
    full_name      TEXT NOT NULL,
    sessions       TEXT NOT NULL,
    institution    TEXT,
    status         TEXT NOT NULL DEFAULT 'CONFIRMED',
    note           TEXT,
    added_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alternate_emails (
    alt_email       TEXT PRIMARY KEY,
    canonical_email TEXT NOT NULL,
    source          TEXT,
    note            TEXT,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (canonical_email) REFERENCES panelists(email)
);
CREATE INDEX IF NOT EXISTS idx_alt_canonical ON alternate_emails(canonical_email);

CREATE VIEW IF NOT EXISTS registrations_corrected AS
SELECT
    r.response_id,
    r.create_time,
    COALESCE(c_email.corrected_value, r.email)               AS email,
    r.first_name,
    r.last_name,
    COALESCE(c_inst.corrected_value, r.institution)          AS institution,
    r.role,
    r.department,
    COALESCE(c_fee.corrected_value, r.fee_category)          AS fee_category,
    COALESCE(c_attend.corrected_value, r.attend_type)        AS attend_type,
    r.experience_level,
    r.tools_used,
    r.primary_area,
    r.topic_wanted,
    r.question_for_conference,
    COALESCE(c_diet.corrected_value, r.dietary_restrictions) AS dietary_restrictions,
    COALESCE(c_acc.corrected_value, r.accessibility_needs)   AS accessibility_needs,
    r.heard_via,
    r.raw_answers_json,
    r.synced_at
FROM registrations r
LEFT JOIN corrections c_email  ON c_email.response_id  = r.response_id AND c_email.field  = 'email'
LEFT JOIN corrections c_attend ON c_attend.response_id = r.response_id AND c_attend.field = 'attend_type'
LEFT JOIN corrections c_inst   ON c_inst.response_id   = r.response_id AND c_inst.field   = 'institution'
LEFT JOIN corrections c_fee    ON c_fee.response_id    = r.response_id AND c_fee.field    = 'fee_category'
LEFT JOIN corrections c_diet   ON c_diet.response_id   = r.response_id AND c_diet.field   = 'dietary_restrictions'
LEFT JOIN corrections c_acc    ON c_acc.response_id    = r.response_id AND c_acc.field    = 'accessibility_needs'
WHERE r.response_id NOT IN (SELECT response_id FROM excluded_duplicates);

CREATE VIEW IF NOT EXISTS panelist_registration_status AS
SELECT
  p.full_name,
  p.email    AS canonical_email,
  p.sessions,
  p.status   AS panelist_status,
  CASE WHEN r.response_id IS NULL THEN 'NOT REGISTERED' ELSE 'REGISTERED' END AS registration,
  r.email    AS registered_as,
  CASE WHEN r.response_id IS NULL THEN NULL
       WHEN trim(coalesce(r.attend_type,''))='' THEN 'In person (pre-dates field)'
       ELSE r.attend_type END AS attend_type
FROM panelists p
LEFT JOIN alternate_emails a ON a.canonical_email = p.email
LEFT JOIN registrations_corrected r
       ON lower(trim(r.email)) = p.email
       OR lower(trim(r.email)) = a.alt_email
GROUP BY p.email;
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


def migrate_existing_columns(conn):
    """Add any new columns to a pre-existing registrations table (ALTER TABLE,
    since CREATE TABLE IF NOT EXISTS won't touch an already-existing table)."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(registrations)")}
    new_cols = {
        "attend_type": "TEXT",
        "tools_used": "TEXT",
        "topic_wanted": "TEXT",
        "question_for_conference": "TEXT",
        "dietary_restrictions": "TEXT",
        "accessibility_needs": "TEXT",
    }
    for col, coltype in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE registrations ADD COLUMN {col} {coltype}")


def main():
    responses = fetch_responses()

    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    migrate_existing_columns(conn)

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
                institution, role, department, fee_category, attend_type,
                experience_level, tools_used, primary_area, topic_wanted,
                question_for_conference, dietary_restrictions,
                accessibility_needs, heard_via, raw_answers_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(response_id) DO UPDATE SET
                create_time=CASE WHEN trim(coalesce(excluded.create_time,'')) <> '' THEN excluded.create_time ELSE registrations.create_time END,
                email=CASE WHEN trim(coalesce(excluded.email,'')) <> '' THEN excluded.email ELSE registrations.email END,
                first_name=CASE WHEN trim(coalesce(excluded.first_name,'')) <> '' THEN excluded.first_name ELSE registrations.first_name END,
                last_name=CASE WHEN trim(coalesce(excluded.last_name,'')) <> '' THEN excluded.last_name ELSE registrations.last_name END,
                institution=CASE WHEN trim(coalesce(excluded.institution,'')) <> '' THEN excluded.institution ELSE registrations.institution END,
                role=CASE WHEN trim(coalesce(excluded.role,'')) <> '' THEN excluded.role ELSE registrations.role END,
                department=CASE WHEN trim(coalesce(excluded.department,'')) <> '' THEN excluded.department ELSE registrations.department END,
                fee_category=CASE WHEN trim(coalesce(excluded.fee_category,'')) <> '' THEN excluded.fee_category ELSE registrations.fee_category END,
                attend_type=CASE WHEN trim(coalesce(excluded.attend_type,'')) <> '' THEN excluded.attend_type ELSE registrations.attend_type END,
                experience_level=CASE WHEN trim(coalesce(excluded.experience_level,'')) <> '' THEN excluded.experience_level ELSE registrations.experience_level END,
                tools_used=CASE WHEN trim(coalesce(excluded.tools_used,'')) <> '' THEN excluded.tools_used ELSE registrations.tools_used END,
                primary_area=CASE WHEN trim(coalesce(excluded.primary_area,'')) <> '' THEN excluded.primary_area ELSE registrations.primary_area END,
                topic_wanted=CASE WHEN trim(coalesce(excluded.topic_wanted,'')) <> '' THEN excluded.topic_wanted ELSE registrations.topic_wanted END,
                question_for_conference=CASE WHEN trim(coalesce(excluded.question_for_conference,'')) <> '' THEN excluded.question_for_conference ELSE registrations.question_for_conference END,
                dietary_restrictions=CASE WHEN trim(coalesce(excluded.dietary_restrictions,'')) <> '' THEN excluded.dietary_restrictions ELSE registrations.dietary_restrictions END,
                accessibility_needs=CASE WHEN trim(coalesce(excluded.accessibility_needs,'')) <> '' THEN excluded.accessibility_needs ELSE registrations.accessibility_needs END,
                heard_via=CASE WHEN trim(coalesce(excluded.heard_via,'')) <> '' THEN excluded.heard_via ELSE registrations.heard_via END,
                raw_answers_json=CASE WHEN trim(coalesce(excluded.raw_answers_json,'')) <> '' THEN excluded.raw_answers_json ELSE registrations.raw_answers_json END,
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
                a.get("How will you attend?", ""),
                a.get("2. How would you describe your experience with computational or AI methods in historical research?", ""),
                a.get("3. Which of the following methods or tools have you used in your research?", ""),
                a.get("4. What is your primary research area or period?", ""),
                a.get("5. Is there a specific session topic, tool, or question you would most like to see addressed at the conference?", ""),
                a.get("6. Is there a question you would like to ask the whole conference?", ""),
                a.get("Dietary restrictions or food allergies", ""),
                a.get("Accessibility needs", ""),
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
