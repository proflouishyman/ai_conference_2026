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
