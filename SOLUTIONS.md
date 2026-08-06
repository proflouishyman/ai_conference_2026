# SOLUTIONS.md

Bug fix log for the AI and History Conference 2026 project.

---

[2026-08-06] - Calendar invites silently corrupted by `send meeting`; two speakers never actually confirmed for calls the next morning

## Problem
Louis flagged that Jim Clifford's scheduling call (supposedly Friday, Aug 7, 10:00-10:30 AM) did not appear on his calendar, despite an earlier session having logged the invite as created and verified. Investigation found the same was true for Kwok Leong Tang's call (Friday, Aug 7, 9:00 AM) and, additionally and previously undetected, for the Kalani Craig / Jeff McClurken call (Tuesday, Aug 11, 10:00 AM).

## Root Cause
`~/.claude/skills/outlook-correspondence/scripts/create_meeting.applescript` called Outlook's `send meeting` command after setting an event's properties and adding an attendee. `send meeting` silently discards every AppleScript property write made before it: the write lands only in Outlook's uncommitted in-memory copy of the new item, and `send meeting` forces the item to re-materialize from its durable/server representation, which never received the correction. The script's own verification step read back the properties in the *same* process, immediately after writing them — a read-your-own-writes fallacy that could only ever confirm the write was accepted, never that it survived. Confirmed by direct A/B: an event created by an earlier version of the script that never called `send meeting` survived intact; four separate events created by the `send meeting` version all reverted to Outlook's bare default (blank subject, blank body, start time reset to the next round half-hour after the script ran — exact match across all four). This also silently broke the companion safety rule ("always send a direct email in addition to a calendar invite") for the same window: the confirmation emails for Clifford, Tang, and Kalani/McClurken were all still sitting unsent in the Outbox, each referencing a calendar invite that did not actually exist.

## Solution
- Removed the `send meeting` call from `create_meeting.applescript` entirely. It now creates a plain, attendee-less calendar block on Louis's own calendar only, and returns immediately without in-process verification.
- Verification now must happen in a genuinely separate `osascript` invocation, after a real delay, checking the result against intent (e.g. weekday match) rather than against the parsed variable.
- Human notification of any scheduled call now goes out exclusively via a direct plain-text email (`reply_mail.applescript` / `send_mail.applescript`) — this was already a standing rule for a different reason (see SKILL.md, "Never let a calendar invite be the only confirmation") but is now load-bearing for a second, independent reason.
- Deleted the four corrupted calendar events (ids 5819, 5820, 5821, 5823) and recreated correct plain-block events for Tang (Fri Aug 7, 9:00-9:30 AM), Clifford (Fri Aug 7, 10:00-10:30 AM), and Kalani/McClurken (Tue Aug 11, 10:00-10:30 AM) using the fixed script, then verified each in a separate later `osascript` call.
- Deleted the three stale queued Outbox emails that falsely claimed a calendar invite had been sent (to Clifford, Tang, and Kalani/McClurken), and sent fresh direct confirmation emails to each stating the day/time in plain text and acknowledging the earlier broken invite.
- Updated `~/.claude/skills/outlook-correspondence/SKILL.md`'s calendar-invite section to remove the incorrect prior guidance (which recommended calling `send meeting` and verifying in-process) and document the confirmed root cause.

## Notes
- This bug shipped twice (2026-08-03, then again 2026-08-04) because the one clean test of the "fixed" script was deleted 4 seconds after creation, so the revert window was never actually observed. Any future change to calendar-invite handling in this project must be checked with a delayed, separate-process read before being trusted, not just a same-invocation read.
- `create_meeting.applescript` also silently accepted ISO-8601-style date strings and parsed them into nonsense dates without erroring (tested: `2026-08-07T10:00:00` evaluates to October 8, year 12175). The script now rejects any start/end string containing `T` or starting `20` before parsing.
- Event id 5808 (Wright Kennedy's call, created before `send meeting` was introduced) was the control that made the A/B comparison possible — it still carries the wrong Zoom room (`https://jh.zoom.us/j/2779901057` instead of `https://jh.zoom.us/my/lhyman`), a separate pre-existing issue, noted but not fixed here since that call already happened.

---

[2026-08-06] - Fresh confirmation emails also stuck unsent for 45+ minutes; second, independent root cause found in the same session

## Problem
After fixing the calendar bug above and sending fresh direct confirmation emails to Clifford, Tang, and Jeff McClurken via `reply_mail.applescript`, those emails themselves sat stuck in the Outbox for 45+ minutes with a `time sent` stamp already set but never actually delivered — the same "looks sent but isn't" symptom as the calendar bug, in a completely different code path. One older stuck message (a reply to Steve Ruggles) had been sitting unsent since 2026-08-04, over 2 days.

## Root Cause
Outlook's `reply to` AppleScript command defaults its `opening window` parameter to `true` (per `Outlook.sdef`: "Should the reply message be opened in a window? Default is to show the window."). `reply_mail.applescript` never passed `opening window:false`, so every reply it ever created silently opened a real, persistent compose window in Outlook. Confirmed directly: `tell application "System Events" to tell process "Microsoft Outlook" to name of every window` showed 4 open windows with names exactly matching the 4 stuck Outbox subjects. Calling `send` on the underlying message object still stamped `time sent`, but the message could not actually leave the Outbox while its window sat open and unclosed. A `Sync Errors` dialog window was also found open at the same time, independently contributing to the same stuck-outbox symptom. Outlook's AppleScript-level `sync <account>` (used internally by `check_outbox.applescript`) does not reliably flush the Outbox even once the blocking windows are gone — the only thing confirmed to actually work is the real GUI "Send & Receive" command.

## Solution
- Fixed `reply_mail.applescript` to pass `opening window:false` to the `reply to` command, so new replies no longer open a window at all. Verified with a live test: window count was identical (2 before, 2 after) creating a reply with the fix, versus a window appearing every time without it.
- Added `~/.claude/skills/outlook-correspondence/scripts/force_send_receive.applescript`, which closes any orphaned Outlook windows (via Outlook's own `close` command on the window object, not simulated clicks) and then drives the real GUI "Send & Receive" command via System Events — the only mechanism confirmed to actually flush a stalled Outbox. It captures whatever app was frontmost before running and restores it immediately after, so the focus steal is momentary.
- Ran the fix live: closed the 4 orphaned windows plus the "Sync Errors" dialog, triggered Send & Receive, and the Outbox went from 4 pending to completely empty. All 7 recently-queued messages (2 fresh reminder emails plus 5 reply-based confirmations, including the 2+ day old Ruggles message) were confirmed present in Sent Items afterward.
- Updated `~/.claude/skills/outlook-correspondence/SKILL.md`'s "If a send comes back UNCONFIRMED" section, which previously recommended against ever using System Events / UI scripting based on an earlier, less complete 2026-08-03 attempt. The complete picture: UI scripting was not the wrong approach, it was missing the orphaned-window diagnosis — closing windows first, then triggering Send & Receive, is what actually works.

## Notes
- Do not run `force_send_receive.applescript` as part of an unattended routine background check — it is a real, visible, momentary focus-steal and should only run when the user is present and aware, exactly as it was used here (the user explicitly asked for it live).
- Any calendar or reply script written by hand rather than using the existing skill scripts should explicitly pass `opening window:false` on any `reply to` call, and should never call `send meeting` on a calendar event (see the entry above).
- Two independent bugs (this one and the calendar `send meeting` bug above) both produced the exact same symptom — a `time sent`/verified-looking success that wasn't real — via the same underlying mechanism class: an AppleScript command that returns success based on an in-memory or just-created object state, while a UI-level artifact (an open window, in both cases) silently prevents the actual durable operation from completing. Treat any future "verified success" report from a scripted Outlook operation with the same suspicion until independently re-confirmed after a delay, in a separate process, against the user's own observation.
