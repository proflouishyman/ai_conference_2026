#!/usr/bin/env python3
"""Watch Canvas Inbox for student replies to the paid-volunteer message and
build a draft volunteer schedule. READ-ONLY on Canvas: never sends or marks
anything. Only ever emails Louis. All student data lives under docs/volunteers/
(gitignored); this file holds no student names or addresses.

  --once        single pass (default behaviour; the flag exists for launchd)
  --dry-run     no email, print what would be sent
  --rebuild     re-parse every stored reply with the LLM
  --synthetic F read conversations from JSON file F instead of Canvas
  --state-dir D keep state/xlsx in D (for tests; default docs/volunteers)
"""
import argparse, datetime as dt, html, json, os, re, socket, sys
import urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANVAS = "https://jhu.instructure.com/api/v1"
TOKEN_FILE = "/Users/louishyman/coding/canvas_api.txt"
LOUIS_ID = 103351
COURSES = {"course_134397": "Intro to Computational History", "course_131917": "STOTE I"}
SUBJECT = "Paid student help at the AI and History Conference"
MODEL = "gpt-5.4-mini"
def _to():
    """Louis's address lives in gitignored scripts/private_overrides.json (or DIGEST_TO)."""
    if os.environ.get("DIGEST_TO"):
        return os.environ["DIGEST_TO"]
    try:
        return json.load(open(os.path.join(ROOT, "scripts", "private_overrides.json")))["digest_to"]
    except Exception:
        return ""


TO = _to()
RATE = 15
JOBS = ["stream", "interviews", "tables"]
DAYS = {"Thu": "Thursday, October 15", "Fri": "Friday, October 16"}


def log(*a):
    print(dt.datetime.now().strftime("%F %T"), *a, flush=True)


# ------------------------------------------------------------------ env
def load_env():
    try:
        for line in open(os.path.expanduser("~/.conference_bots.env")):
            line = re.sub(r"^export\s+", "", line.strip())
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass
    if not os.environ.get("OPENAI_API_KEY"):
        try:
            k = json.load(open(os.path.expanduser("~/.claude/settings.json"))
                          ).get("env", {}).get("OPENAI_API_KEY", "")
            if k:
                os.environ["OPENAI_API_KEY"] = k
        except Exception:
            pass


# ------------------------------------------------------------------ canvas (GET only)
def canvas_token():
    t = open(TOKEN_FILE).read().strip()
    t = t.split("=", 1)[-1].strip() if "=" in t[:30] else t
    return re.sub(r"^Bearer\s+", "", t).strip("'\"")


def canvas_get(path, token):
    """GET with Link-header paging. Returns concatenated list (or dict)."""
    url = path if path.startswith("http") else CANVAS + path
    out = None
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
            link = r.headers.get("Link", "")
        if isinstance(data, list):
            out = (out or []) + data
        else:
            return data
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return out or []


def fetch_conversations():
    token = canvas_token()
    seen = {}
    for scope in ("inbox", "sent"):
        for c in canvas_get(f"/conversations?scope={scope}&per_page=100", token):
            if SUBJECT.lower() in (c.get("subject") or "").lower():
                seen[c["id"]] = c
    full = []
    for cid in seen:
        full.append(canvas_get(f"/conversations/{cid}", token))
    return full


# ------------------------------------------------------------------ replies
def extract_replies(convs):
    """-> {student_id: {name, course, conversation_id, messages:[{id,text,time}]}}"""
    studs = {}
    for c in convs:
        parts = {p["id"]: p.get("name", "?") for p in c.get("participants", [])}
        code = c.get("context_code") or ""
        for m in c.get("messages", []):
            if m.get("author_id") == LOUIS_ID:
                continue
            uid = m["author_id"]
            s = studs.setdefault(str(uid), {
                "name": parts.get(uid, "?"), "canvas_user_id": uid,
                "course": COURSES.get(code, c.get("context_name") or code),
                "conversation_id": c["id"], "messages": []})
            s["messages"].append({"id": m["id"], "text": (m.get("body") or "").strip(),
                                  "time": m.get("created_at")})
    for s in studs.values():
        s["messages"].sort(key=lambda x: x["time"] or "")
    return studs


# ------------------------------------------------------------------ LLM parse
PARSE_PROMPT = """You read a college student's reply to a message offering paid help at a two-day conference (Thursday Oct 15 and Friday Oct 16, 2026). Jobs offered: "stream" (monitoring Zoom webstreams), "interviews" (informal man-on-the-street video interviews), "tables" (moving tables / room setup). Return ONLY JSON:
{"interested": "yes"|"no"|"maybe",
 "availability": [{"day":"Thu"|"Fri","start":"HH:MM","end":"HH:MM"}],
 "jobs": ["stream","interviews","tables"] subset, or "any" if they say any/all/whatever,
 "notes": "short string",
 "questions_for_louis": ["each question they asked"]}
Rules: 24-hour times. Conference hours are roughly 08:00-20:00, so "all day Thursday" = 08:00-20:00, "after 2pm Friday" = 14:00-20:00, "morning" = 08:00-12:00, "afternoon" = 12:00-17:00, "before noon" = 08:00-12:00. Bare hours like "1-3" mean afternoon. If they say they are free both days with no times, give both days 08:00-20:00. If no availability is stated, use []. If no job preference is stated, jobs = "any". Several messages from the same student are given in order; later ones override earlier ones."""


def llm_parse(texts):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        log("OPENAI_API_KEY missing; reply left unparsed")
        return None
    body = {"model": MODEL, "response_format": {"type": "json_object"},
            "max_completion_tokens": 600,
            "messages": [{"role": "system", "content": PARSE_PROMPT},
                         {"role": "user", "content": "\n---\n".join(t[:2000] for t in texts)}]}
    for attempt in range(3):
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
            method="POST", headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return normalise(json.loads(json.load(r)["choices"][0]["message"]["content"]))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                import time; time.sleep(3 * (attempt + 1)); continue
            log("LLM HTTP", e.code); return None
        except Exception as e:
            log("LLM error", str(e)[:120]); return None


def hm(s):
    m = re.match(r"^(\d{1,2}):?(\d{2})?$", str(s).strip())
    return int(m.group(1)) * 60 + int(m.group(2) or 0) if m else None


def normalise(p):
    """Validate LLM output into the canonical shape."""
    p = p if isinstance(p, dict) else {}
    interested = str(p.get("interested", "maybe")).lower()
    if interested not in ("yes", "no", "maybe"):
        interested = "maybe"
    av = []
    for a in p.get("availability") or []:
        try:
            day = str(a["day"])[:3].title()
            s, e = hm(a["start"]), hm(a["end"])
            if day in DAYS and s is not None and e is not None and e > s:
                av.append({"day": day, "start": a["start"], "end": a["end"]})
        except Exception:
            pass
    jobs = p.get("jobs", "any")
    if isinstance(jobs, str):
        jobs = "any" if jobs.lower() == "any" else [jobs]
    jobs = [j for j in jobs or [] if j in JOBS]
    if not jobs:
        jobs = "any"
    q = p.get("questions_for_louis") or []
    q = [q] if isinstance(q, str) else [str(x) for x in q if x]
    return {"interested": interested, "availability": av, "jobs": jobs,
            "notes": str(p.get("notes") or ""), "questions_for_louis": q}


# ------------------------------------------------------------------ program -> shifts
def fmt(m):
    h, mi = divmod(m, 60)
    return f"{(h % 12) or 12}:{mi:02d}{'am' if h < 12 else 'pm'}"


def tmin(h, m):
    h, m = int(h), int(m)
    return (h + 12 if h < 8 else h) * 60 + m   # program uses 12h clock without am/pm


def parse_program(path=None):
    """Rows of index.html -> {'Thu':[...], 'Fri':[...]} each row {s,e,title,room,num}."""
    s = open(path or os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    out = {}
    for day, pid in (("Thu", "panel-day1"), ("Fri", "panel-day2")):
        i = s.find(f'id="{pid}"')
        j = s.find('class="day-panel', i + 10)
        seg = s[i:j if j > 0 else len(s)]
        rows = []
        for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr.group(1), re.S)
            if len(tds) < 4:
                continue
            clean = []
            for t in tds:
                t = re.sub(r'<div class="session-desc">.*?</div>', "", t, flags=re.S)
                t = re.sub(r'<span class="session-presenter">.*?</span>', "", t, flags=re.S)
                clean.append(" ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split()))
            tm = re.match(r"(\d+):(\d+)\s*[–-]\s*(\d+):(\d+)", clean[0])
            if not tm:
                continue
            a, b = tmin(tm[1], tm[2]), tmin(tm[3], tm[4])
            nm = re.match(r"(\d+)\s+(.*)", clean[1])
            title = (nm.group(2) if nm else clean[1]).replace(" #", "").strip()
            rows.append({"s": a, "e": b, "num": int(nm.group(1)) if nm else None,
                         "title": title[:60], "room": clean[3]})
        out[day] = rows
    return out


def build_shifts(prog):
    shifts = []

    def add(day, s, e, job, label, need=1, optional=False):
        shifts.append({"id": len(shifts) + 1, "day": day, "s": s, "e": e, "job": job,
                       "label": label, "need": need, "optional": optional})

    for day, rows in prog.items():
        # --- stream: one monitor per room per contiguous run of numbered sessions
        sess = sorted([r for r in rows if r["num"]], key=lambda r: (r["room"], r["s"]))
        runs = []
        for r in sess:
            if (runs and runs[-1]["room"] == r["room"] and runs[-1]["e"] == r["s"]
                    and runs[-1]["num"] not in (28,) and r["num"] != 28):
                runs[-1]["e"] = r["e"]; runs[-1]["nums"].append(r["num"])
            else:
                runs.append({"room": r["room"], "s": r["s"], "e": r["e"],
                             "num": r["num"], "nums": [r["num"]]})
        for r in sorted(runs, key=lambda r: (r["s"], r["room"])):
            nums = ",".join("#%d" % n for n in r["nums"])
            add(day, r["s"], r["e"], "stream", f"{r['room']} ({nums})",
                optional=(28 in r["nums"]))
        # --- interviews: 1-hour roaming blocks in breaks and lunch
        for r in rows:
            t = r["title"].lower()
            if r["num"] or not ("coffee break" in t or "lunch" in t or t == "short break"):
                continue
            a = r["s"]
            while a < r["e"]:
                b = min(a + 60, r["e"])
                if b - a >= 30:
                    add(day, a, b, "interviews", "Roaming: " + r["title"].split(" — ")[0])
                a = b
        # --- tables: crew of 3, 30 min before lunch (Thu+Fri) and Thu dinner
        for r in rows:
            t = r["title"].lower()
            meal = ("lunch" in t) or (day == "Thu" and "dinner" in t)
            if meal and not r["num"]:
                add(day, r["s"] - 30, r["s"], "tables",
                    "Set tables before " + r["title"], need=3)
    shifts.sort(key=lambda x: (x["day"] != "Thu", x["s"], x["job"], x["id"]))
    for i, x in enumerate(shifts, 1):
        x["id"] = i
    return shifts


# ------------------------------------------------------------------ assignment
def eligible(stu, sh):
    p = stu["parsed"]
    if not p or p["interested"] == "no":
        return False
    if p["jobs"] != "any" and sh["job"] not in p["jobs"]:
        return False
    return any(a["day"] == sh["day"] and hm(a["start"]) <= sh["s"] and hm(a["end"]) >= sh["e"]
               for a in p["availability"])


def assign(students, shifts):
    """Greedy. Hardest (fewest eligible) slots first; pick least-loaded eligible
    student, 'yes' before 'maybe', no overlaps. Optional shifts go last."""
    load = {k: 0.0 for k in students}
    busy = {k: [] for k in students}
    slots = []
    for sh in shifts:
        for n in range(sh["need"]):
            slots.append({"shift": sh, "n": n + 1, "student": None})
    elig = {sh["id"]: [k for k, s in students.items() if eligible(s, sh)] for sh in shifts}
    slots.sort(key=lambda x: (x["shift"]["optional"], x["shift"]["job"] != "tables",
                              len(elig[x["shift"]["id"]]),
                              x["shift"]["day"] != "Thu", x["shift"]["s"]))
    for sl in slots:
        sh = sl["shift"]
        cands = [k for k in elig[sh["id"]]
                 if not any(d == sh["day"] and sh["s"] < e and s < sh["e"] for d, s, e in busy[k])]
        if not cands:
            continue
        k = min(cands, key=lambda k: (students[k]["parsed"]["interested"] != "yes",
                                      load[k], k))
        sl["student"] = k
        load[k] += (sh["e"] - sh["s"]) / 60
        busy[k].append((sh["day"], sh["s"], sh["e"]))
    slots.sort(key=lambda x: (x["shift"]["id"], x["n"]))
    return slots


def coverage(slots):
    req = [s for s in slots if not s["shift"]["optional"]]
    return sum(1 for s in req if s["student"]), sum(1 for s in req if not s["student"])


# ------------------------------------------------------------------ xlsx
def write_xlsx(path, students, shifts, slots):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    bold = Font(bold=True)
    red = PatternFill("solid", fgColor="F8CBAD")

    def sheet(title, header, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font = bold
        for r in rows:
            ws.append(r)
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(
                60, max(10, max(len(str(c.value or "")) for c in col) + 2))
        ws.freeze_panes = "A2"
        return ws

    name = lambda k: students[k]["name"] if k else "UNFILLED"
    rows = [[s["shift"]["id"], DAYS[s["shift"]["day"]].split(",")[0],
             fmt(s["shift"]["s"]), fmt(s["shift"]["e"]), s["shift"]["job"],
             s["shift"]["label"], s["n"], name(s["student"]),
             "optional" if s["shift"]["optional"] else ""] for s in slots]
    ws = sheet("Schedule", ["Shift", "Day", "Start", "End", "Job", "Where/what", "Slot",
                            "Student", "Note"], rows, first=True)
    for r in ws.iter_rows(min_row=2):
        if r[7].value == "UNFILLED":
            for c in r:
                c.fill = red

    hours, cnt = {}, {}
    for s in slots:
        if s["student"]:
            h = (s["shift"]["e"] - s["shift"]["s"]) / 60
            hours[s["student"]] = hours.get(s["student"], 0) + h
            cnt[s["student"]] = cnt.get(s["student"], 0) + 1
    srows = []
    for k, st in students.items():
        p = st["parsed"] or {}
        srows.append([st["name"], st["course"], p.get("interested", "unparsed"),
                      cnt.get(k, 0), hours.get(k, 0),
                      "; ".join(f"{a['day']} {a['start']}-{a['end']}" for a in p.get("availability", [])),
                      p.get("jobs") if p.get("jobs") == "any" else ", ".join(p.get("jobs", []))])
    sheet("By student", ["Student", "Course", "Interested", "Shifts", "Hours",
                         "Availability", "Jobs wanted"], srows)

    un = [[s["shift"]["id"], DAYS[s["shift"]["day"]].split(",")[0], fmt(s["shift"]["s"]),
           fmt(s["shift"]["e"]), s["shift"]["job"], s["shift"]["label"], s["n"]]
          for s in slots if not s["student"] and not s["shift"]["optional"]]
    sheet("Unfilled", ["Shift", "Day", "Start", "End", "Job", "Where/what", "Slot"], un)

    rrows = []
    for k, st in students.items():
        p = st["parsed"] or {}
        rrows.append([st["name"], st["course"], st["messages"][-1]["time"],
                      "\n".join(m["text"] for m in st["messages"]), p.get("interested", "unparsed"),
                      json.dumps(p.get("availability", [])),
                      p.get("jobs") if p.get("jobs") == "any" else ", ".join(p.get("jobs", [])),
                      p.get("notes", ""), " | ".join(p.get("questions_for_louis", []))])
    sheet("Replies", ["Student", "Course", "Last reply", "Text", "Interested", "Availability",
                      "Jobs", "Notes", "Questions"], rrows)

    prow = [[students[k]["name"], hours[k], RATE, f"=B{i}*C{i}"]
            for i, k in enumerate(sorted(hours, key=lambda k: students[k]["name"]), 2)]
    ws = sheet("Payroll estimate", ["Student", "Hours", "Rate ($/hr)", "Pay ($)"], prow)
    n = len(prow) + 1
    ws.append(["TOTAL", f"=SUM(B2:B{n})", "", f"=SUM(D2:D{n})"])
    for c in ws[n + 1]:
        c.font = bold
    wb.save(path)


# ------------------------------------------------------------------ email (Louis only)
def send_email(subject, body, dry):
    if dry:
        log("DRY-RUN would email Louis |", subject)
        print("-" * 60, "\n" + body, "\n" + "-" * 60)
        return True
    try:
        if not TO:
            raise RuntimeError("no recipient (digest_to) configured")
        sys.path.insert(0, os.path.expanduser("~/coding/agora_media/scripts"))
        from send_digest_email import send
        from meltwater_client import load_env as agora_env
        env = agora_env()
        addr, pw = env.get("GMAIL_ADDRESS"), env.get("GMAIL_APP_PASSWORD")
        if not addr or not pw:
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing")
        old = socket.getdefaulttimeout(); socket.setdefaulttimeout(30)
        try:
            send([TO], subject, body, addr, pw)
        finally:
            socket.setdefaulttimeout(old)
        log("emailed Louis |", subject)
        return True
    except Exception as e:
        log("EMAIL FAILED:", str(e)[:160])
        return False


def describe(st):
    p = st["parsed"]
    if not p:
        return "  (not parsed; read the reply)"
    av = "; ".join(f"{a['day']} {a['start']}-{a['end']}" for a in p["availability"]) or "none stated"
    jobs = p["jobs"] if p["jobs"] == "any" else ", ".join(p["jobs"])
    return f"  Interested: {p['interested']} | Free: {av} | Jobs: {jobs}" + (
        f"\n  Notes: {p['notes']}" if p["notes"] else "")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--synthetic")
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "docs", "volunteers"))
    a = ap.parse_args()
    load_env()
    os.makedirs(a.state_dir, exist_ok=True)
    sp = os.path.join(a.state_dir, "volunteers.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {"students": {}}

    try:
        convs = json.load(open(a.synthetic)) if a.synthetic else fetch_conversations()
    except Exception as e:
        log("Canvas fetch failed:", str(e)[:160]); return 1
    found = extract_replies(convs)
    log(f"{len(convs)} matching conversations, {len(found)} students have replied")

    new = []
    for uid, f in found.items():
        cur = state["students"].get(uid)
        known = {m["id"] for m in cur["messages"]} if cur else set()
        fresh = [m for m in f["messages"] if m["id"] not in known]
        if cur:
            cur["messages"] = f["messages"]
        else:
            cur = state["students"][uid] = {**f, "parsed": None}
        if fresh or a.rebuild or cur["parsed"] is None:
            cur["parsed"] = llm_parse([m["text"] for m in cur["messages"]])
        if fresh:
            new.append((uid, fresh))

    students = state["students"]
    shifts = build_shifts(parse_program())
    slots = assign(students, shifts)
    xp = os.path.join(a.state_dir, "volunteer_schedule.xlsx")
    write_xlsx(xp, students, shifts, slots)
    state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    interested = sum(1 for s in students.values() if (s["parsed"] or {}).get("interested") in ("yes", "maybe"))
    filled, unfilled = coverage(slots)
    log(f"{len(students)} replies on file, {interested} interested, "
        f"{filled} slots filled, {unfilled} unfilled, {len(shifts)} shifts")

    def save():
        if not a.dry_run:          # a dry run never records replies as seen
            json.dump(state, open(sp, "w"), indent=1)

    if not new:
        save(); return 0
    lines = []
    for uid, fresh in new:
        st = students[uid]
        quote = " ".join(fresh[-1]["text"].split())[:240]
        lines += [f"{st['name']} ({st['course']})", describe(st), f'  "{quote}"', ""]
    qs = [(students[u]["name"], q) for u, _ in new
          for q in ((students[u]["parsed"] or {}).get("questions_for_louis") or [])]
    body = ["NEW REPLIES", ""] + lines
    body += ["QUESTIONS FOR YOU", ""] + (
        [f"- {n}: {q}" for n, q in qs] if qs else ["(none)"]) + [""]
    body += [f"COVERAGE (draft): {filled} slots filled, {unfilled} unfilled, "
             f"{sum(1 for x in slots if not x['shift']['optional'])} required slots in all.", ""]
    for s in slots:
        if not s["student"] and not s["shift"]["optional"]:
            sh = s["shift"]
            body.append(f"  UNFILLED: {sh['day']} {fmt(sh['s'])}-{fmt(sh['e'])} {sh['job']} {sh['label']}"
                        f" (slot {s['n']})")
    body += ["", "Schedule workbook: " + xp, "", "No messages have been sent to students."]
    subj = f"Volunteer replies: {len(new)} new (total {interested} interested)"
    ok = send_email(subj, "\n".join(body), a.dry_run)
    if ok and not a.dry_run:
        state["notified"] = dt.datetime.now().isoformat(timespec="seconds")
    elif not ok:
        # leave message ids unrecorded so the next run re-notifies
        for uid, fresh in new:
            gone = {m["id"] for m in fresh}
            students[uid]["messages"] = [m for m in students[uid]["messages"] if m["id"] not in gone]
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
