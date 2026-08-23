# AI and History Conference 2026 — project orientation

Read this first, every session, before touching anything in this repo.

## Startup checklist

1. **Read `OUTREACH.md` top to bottom, or at least the Session Grid at the top.** It is the
   single source of truth for who's confirmed, who's pending, and every session's current
   day/time/room. Do not trust memory of prior conversations for this — the grid changes often
   (session moves, new confirmations, declines) and stale assumptions cause real mistakes (e.g.
   emailing someone about a slot that already moved).
2. **Check for anything overdue or time-sensitive** — search OUTREACH.md for "not yet" / "needs"
   / "overdue" / "time-sensitive" to surface loose ends (e.g. a courtesy note that's been owed to
   someone for days).
3. **For anything involving email or calendar, use the `outlook-correspondence` skill.** Say so
   explicitly ("use the outlook-correspondence skill for this") to remove any ambiguity about
   invoking it. The skill itself requires reading `~/coding/email/README.md` (orientation: mail
   index vs. this skill vs. the OpenClaw bridge, calendar rules, writing style) and
   `~/coding/email/contacts.md` (open threads, what's already been sent to whom) before doing
   anything — so invoking the skill pulls in the rest of the system automatically. No separate
   instruction to go read those files first is needed.
4. **If asked to send anything**, the skill's own flow (and the OpenClaw send-approval carve-out
   it documents) covers draft, approve, and delivery verification (Sent Items count / message ID)
   in one pass — do not wait for a second confirmation once Louis has asked for the send. See
   global CLAUDE.md rule 23 and the memory note `feedback_email_send_no_double_ask`.

## What this project is

Conference website + speaker-outreach tracker for the AI and History Conference 2026, October
15-16, Johns Hopkins University (SNF Agora), run in partnership with the AHA. Live site:
https://proflouishyman.github.io/ai_conference_2026

## Key files

- `index.html`, `register.html`, `thanks.html`, `hotels.html`, `getting-here.html` — the public
  site (see `README.md` for full file breakdown and deploy instructions).
- `OUTREACH.md` — speaker outreach tracker. The session grid at the top is the fastest way to see
  current state; individual numbered entries below it have full correspondence history per
  speaker, including emails, honorarium terms, and travel-funding notes.
- `MAILING_LIST.md` / `.csv` — broader candidate pool, feeds into OUTREACH.md as people get
  promoted to active outreach.
- `PRESENTER_GUIDELINES.md` — pitched technical, not softened (see memory
  `feedback_presentation_level`).
- `SCHEDULE_FILLING_PLAN.md` — planning notes for the program grid.
- `correspondence/` — local read-only archive of real email threads relevant to outreach.

## CC policy

**CC Young Song (ysong@jhu.edu) only on logistics correspondence** — food, rooms/venue space,
hotels, and similar physical/event-logistics matters — narrowed 2026-08-13 (revised same day) at
Young's own request. Do **not** CC her on program-planning correspondence: speaker invitations,
session content/structure, honorariums, confirmations, or status updates about who's speaking.
For program-planning threads, CC Em Cytrynbaum (ecytryn1@jhu.edu) instead, per the Young/Em
ownership split (Young owns logistics, Em owns speakers/honorarium side).

## Standing preferences specific to this project

- **Tim O'Reilly is not a "conference presenter."** He delivers the Annual Margaret Levi Lecture
  in Political Economy, a standalone event timed alongside the conference to draw a crowd (same
  pattern as last year), not formally part of the conference program. Whenever Louis says "all
  conference presenters/speakers" (e.g. mass emails, registration reminders, panelist intros), do
  not include him — he is on OUTREACH.md's Session Grid for scheduling purposes only.
- No international speakers except explicit, approved exceptions (currently: Jim Clifford,
  Canada-based — see OUTREACH.md entry #37 for the reasoning).
- When a speaker confirms, send a brief thank-you reply, not just a status update.
- Copy-ready emails (plain text, no markdown) per global CLAUDE.md rule 21; invitation emails
  follow the fixed structure in rule 22.
- No em dashes/semicolons, no AI-style rule-of-three lists in any generated prose (global rules
  24 and 27).
