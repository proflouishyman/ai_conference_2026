BEGIN;

-- Survey-reported attendance extent, from the 2026-09-10 blast to local
-- in-person registrants. Kept out of `corrections` because it is not a
-- correction to a registration field: it is a separate, dated fact with its
-- own provenance and a day breakdown that a single corrected_value cannot
-- carry. The sync never touches this table.
CREATE TABLE IF NOT EXISTS attendance_extent (
    response_id  TEXT PRIMARY KEY,
    extent       TEXT NOT NULL CHECK (extent IN (
                     'both_days',      -- 1: both days, eating
                     'one_day',        -- 2: one day, eating
                     'partial',        -- 3: own panel / few sessions, not eating
                     'unsure',         -- 4: not sure yet
                     'withdrawn',      -- no longer attending at all
                     'yielded_seat')), -- attending but giving the seat up
    days         TEXT,                 -- 'oct15', 'oct16', 'both', NULL if n/a
    eats         INTEGER NOT NULL DEFAULT 1,   -- counts toward catering
    holds_seat   INTEGER NOT NULL DEFAULT 1,   -- occupies in-person capacity
    reply_email  TEXT,                 -- address they answered from
    verbatim     TEXT,                 -- their own words, for auditing my read
    source       TEXT NOT NULL DEFAULT 'local-survey-2026-09-10',
    eats_confirmed INTEGER,             -- 1 yes / 0 no / NULL asked-but-unanswered
    notes        TEXT,                  -- detail extent+days cannot carry
    recorded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (response_id) REFERENCES registrations(response_id)
);

CREATE INDEX IF NOT EXISTS idx_extent_extent ON attendance_extent(extent);

COMMIT;

DROP VIEW IF EXISTS effective_attendance_summary;
DROP VIEW IF EXISTS effective_attendance;

CREATE VIEW effective_attendance AS
WITH inperson AS (
  SELECT r.response_id, r.first_name, r.last_name, r.email, r.institution
  FROM registrations_corrected r
  WHERE trim(coalesce(r.attend_type,'')) = '' OR r.attend_type = 'In person'
)
SELECT
  i.response_id, i.first_name, i.last_name, i.email, i.institution,
  coalesce(e.extent,'no_reply')                 AS extent,
  e.days, e.notes,
  coalesce(e.holds_seat, 1)                     AS holds_seat,
  -- 1 = said yes, 0 = said no, NULL = asked but unanswered (incl. no reply yet)
  CASE WHEN e.response_id IS NULL THEN NULL ELSE e.eats_confirmed END AS eats_confirmed,
  CASE WHEN coalesce(e.eats,1)=0 THEN 0
       WHEN e.extent='one_day' AND e.days='oct16' THEN 0 ELSE 1 END AS eats_oct15,
  CASE WHEN coalesce(e.eats,1)=0 THEN 0
       WHEN e.extent='one_day' AND e.days='oct15' THEN 0 ELSE 1 END AS eats_oct16
FROM inperson i
LEFT JOIN attendance_extent e ON e.response_id = i.response_id;

CREATE VIEW effective_attendance_summary AS
SELECT 'Registered in person' AS metric, count(*) AS n FROM effective_attendance
UNION ALL SELECT 'Effective seats held',   sum(holds_seat)   FROM effective_attendance
UNION ALL SELECT 'Seats released',         sum(1-holds_seat) FROM effective_attendance
UNION ALL SELECT 'Meals Oct 15 (max)',     sum(eats_oct15)   FROM effective_attendance
UNION ALL SELECT 'Meals Oct 16 (max)',     sum(eats_oct16)   FROM effective_attendance
UNION ALL SELECT 'Meals confirmed yes',    sum(eats_confirmed=1) FROM effective_attendance
UNION ALL SELECT 'Meals confirmed no',     sum(eats_confirmed=0) FROM effective_attendance
UNION ALL SELECT 'Meals unanswered',       sum(eats_confirmed IS NULL) FROM effective_attendance
UNION ALL SELECT 'Survey replies in',      sum(extent<>'no_reply') FROM effective_attendance
UNION ALL SELECT 'Awaiting reply',         sum(extent='no_reply')  FROM effective_attendance;
