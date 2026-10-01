#!/usr/bin/env python3
"""Create the #sessions forum channel (one post per conference session).

Source of truth: index.html (rows id="session-N").
Idempotent: skips the forum, tags and the #live-sessions pointer if they
already exist. Posts are matched to sessions by TITLE (not number), so a renumbering
renames the thread and rewrites its starter message instead of creating a duplicate. Dry run by default; pass --apply to write.
"""
import argparse, html, json, re, sys, time, urllib.request, urllib.error

TOKEN_PATH = "/Users/louishyman/coding/discord_ai_conference_bot_token.txt"
INDEX = "/Users/louishyman/coding/ai_conference_2026/index.html"
API = "https://discord.com/api/v10"
SITE = "https://proflouishyman.github.io/ai_conference_2026"
TOPIC = ("One post per session. Ask questions and talk about the talk here, "
         "during and after. All times Eastern.")
TAGS = ["Thursday", "Friday", "Morning", "Afternoon", "Digital only", "Hands-on", "Plenary"]
PLENARY_FORCE = {2, 17, 28}
HANDS_ON_FORCE = {5, 10, 16, 18, 25}
HANDS_ON_RE = re.compile(r"hands-on|working session|\blab\b|live demonstration|live walkthrough", re.I)
MAXMSG = 1800


def prose(t):
    """No em dashes or semicolons in prose."""
    t = re.sub(r"\s*—\s*", ", ", t)
    t = re.sub(r"\s*;\s*", ", ", t)
    return t


def clean(frag):
    frag = re.sub(r"<br\s*/?>", "\n", frag)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = html.unescape(frag).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", frag).strip()


def fmt_time(h, m, per):
    return f"{h}:{m}"


def parse_range(txt):
    a, b = [x.strip() for x in re.split(r"[–—-]", txt)]
    (h1, m1), (h2, m2) = [tuple(int(v) for v in x.split(":")) for x in (a, b)]
    # program hours: 8-11 are am, 12 and 1-7 are pm
    def per(h): return "am" if 8 <= h <= 11 else "pm"
    p1, p2 = per(h1), per(h2)
    s = f"{h1}:{m1:02d}"; e = f"{h2}:{m2:02d}"
    key = ((h1 % 12) + (12 if p1 == "pm" else 0)) * 60 + m1
    return s, p1, e, p2, key


def parse():
    s = open(INDEX, encoding="utf-8").read()
    d1, d2 = s.index('id="panel-day1"'), s.index('id="panel-day2"')
    rows = []
    for m in re.finditer(r'<tr class="([^"]*)" id="session-(\d+)">(.*?)</tr>', s, re.S):
        cls, n, body = m.group(1), int(m.group(2)), m.group(3)
        day = "Thursday" if d1 < m.start() < d2 else "Friday"
        tm = html.unescape(re.search(r'time-cell">([^<]+)<', body).group(1))
        st = re.search(r'<span class="session-title">(.*?)<a class="session-link"', body, re.S).group(1)
        digital = "session-digital" in st
        st = re.sub(r'<span class="session-digital">.*?</span>', "", st, flags=re.S)
        st = re.sub(r'<span class="session-num">\d+</span>', "", st)
        title = clean(st)
        pm = re.search(r'<span class="session-presenter">(.*?)</span>', body, re.S)
        dm = re.search(r'<div class="session-desc">(.*?)</div>', body, re.S)
        desc_raw = dm.group(1) if dm else ""
        speakers = None
        if pm:
            speakers = clean(pm.group(1))
        else:
            first = re.split(r"<br\s*/?>", desc_raw, maxsplit=1)
            if len(first) == 2 and "Speakers" in first[0]:
                speakers = re.sub(r"^Speakers:\s*", "", clean(first[0])).replace(" | ", ". ")
                desc_raw = first[1]
        s1, p1, e1, p2, key = parse_range(tm)
        rows.append(dict(n=n, cls=cls, day=day, s=s1, sp=p1, e=e1, ep=p2, key=key,
                         title=title, digital=digital, speakers=speakers,
                         desc=clean(desc_raw), raw_desc=desc_raw))
    rows.sort(key=lambda r: (r["day"] != "Thursday", r["key"], r["n"]))
    return rows


def tags_for(r):
    t = [r["day"], "Morning" if r["sp"] == "am" else "Afternoon"]
    if r["digital"]: t.append("Digital only")
    if r["n"] in HANDS_ON_FORCE or HANDS_ON_RE.search(r["title"] + " " + r["desc"]):
        t.append("Hands-on")
    # Opening/closing remarks (1, 26) are also row-plenary on the page but are not tagged.
    if r["n"] in PLENARY_FORCE or re.search(r"plenary", r["title"] + r["desc"], re.I):
        t.append("Plenary")
    return t


def post_title(r):
    d = "Thu" if r["day"] == "Thursday" else "Fri"
    pre = f"#{r['n']} · {d} {r['s']} {r['sp']} · "
    t = prose(r["title"])
    if len(pre) + len(t) > 100:
        t = t[:100 - len(pre) - 1].rstrip() + "…"
    return pre + t


def trim(desc, room):
    if len(desc) <= room: return desc
    cut = desc[:room]
    ends = [m.end() for m in re.finditer(r"[.!?][\"')\]]?(?=\s|$)", cut)]
    return cut[:ends[-1]].strip() if ends else cut.rsplit(" ", 1)[0].rstrip(",:") + "."


def post_body(r):
    day_n = 15 if r["day"] == "Thursday" else 16
    if r["sp"] == r["ep"]:
        when = f"{r['s']}–{r['e']} {r['ep']} ET"
    else:
        when = f"{r['s']} {r['sp']}–{r['e']} {r['ep']} ET"
    head = [f"**{prose(r['title'])}**", f"{r['day']}, October {day_n}, {when}"]
    if r["speakers"]: head.append(f"Speakers: {prose(r['speakers'])}")
    tail = [f"Program: {SITE}/#session-{r['n']}",
            "Watch online: every session is streamed and recorded. Joining details will be sent to registrants before the conference.",
            "Questions for the speaker? Post them in this thread."]
    fixed = "\n".join(head) + "\n\n" + "\n\n" + "\n".join(tail)
    d = trim(prose(r["desc"]).replace("\n", " "), MAXMSG - len(fixed) - 10)
    return "\n".join(head) + ("\n\n" + d if d else "") + "\n\n" + "\n".join(tail)


class Api:
    def __init__(self):
        self.tok = open(TOKEN_PATH).read().strip()
    def call(self, method, path, body=None):
        while True:
            req = urllib.request.Request(API + path, method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": "Bot " + self.tok, "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (aiconf2026, 1.0)"})
            try:
                with urllib.request.urlopen(req) as resp:
                    t = resp.read()
                    return json.loads(t) if t else None
            except urllib.error.HTTPError as e:
                data = e.read()
                if e.code == 429:
                    time.sleep(float(json.loads(data).get("retry_after", 2)) + 0.5); continue
                sys.exit(f"HTTP {e.code} {method} {path}: {data[:300]!r}")


def safety_check(api, threads):
    """Return [(thread_id, name, author)] for messages by anyone but the bot."""
    me = api.call("GET", "/users/@me")["id"]
    bad = []
    for i, n in threads.items():
        before = ""
        while True:
            msgs = api.call("GET", f"/channels/{i}/messages?limit=100" + (f"&before={before}" if before else ""))
            bad += [(i, n, m["author"].get("username")) for m in msgs if m["author"]["id"] != me]
            if len(msgs) < 100: break
            before = msgs[-1]["id"]
        time.sleep(0.3)
    return bad


def rebuild(api, fid, rows, tid, threads, apply):
    """Recreate every post so creation order matches program order.

    Discord's creation-date sort (default_sort_order=1) lists NEWEST first, so
    #28 is created first and #1 last, leaving #1 on top. Old threads are deleted
    only after all new ones exist, and never if anyone but the bot has posted."""
    bad = safety_check(api, threads)
    print(f"safety check: {len(threads)} threads, {len(bad)} non-bot messages")
    for b in bad: print("  NON-BOT:", b)
    if bad:
        sys.exit("stopping: non-bot messages found, nothing created or deleted")
    if not apply:
        print("dry run: would create", len(rows), "posts in reverse order, delete", len(threads), "old threads"); return
    old = set(threads)
    for r in reversed(rows):
        api.call("POST", f"/channels/{fid}/threads", {
            "name": r["_t"], "applied_tags": [tid[t] for t in r["_tags"]],
            "message": {"content": r["_b"], "allowed_mentions": {"parse": []}}})
        print("created", r["_t"]); time.sleep(2)
    for i in old:
        api.call("DELETE", f"/channels/{i}"); time.sleep(1.5)
    api.call("PATCH", f"/channels/{fid}", {"default_sort_order": 1})
    print("deleted", len(old), "old threads; default_sort_order=1")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="safety-check, recreate all posts in order, delete the old set, sort by creation date")
    ap.add_argument("--show", nargs="*", type=int, default=[5, 27])
    a = ap.parse_args()
    rows = parse()
    assert len(rows) == 28, len(rows)
    for r in rows:
        r["_t"], r["_b"], r["_tags"] = post_title(r), post_body(r), tags_for(r)
        assert len(r["_b"]) < 1800 and len(r["_t"]) <= 100
        assert "—" not in r["_b"] + r["_t"] and ";" not in r["_b"] + r["_t"]
    print(f"{'DRY RUN' if not a.apply else 'APPLY'}: {len(rows)} sessions")
    for r in rows: print(f"  {r['_t']}  {r['_tags']}")
    for n in a.show:
        r = next(x for x in rows if x["n"] == n)
        print(f"\n===== session {n}: title={r['_t']!r}\ntags={r['_tags']}\n{r['_b']}\n(len {len(r['_b'])})")

    api = Api()
    gid = api.call("GET", "/users/@me/guilds")[0]["id"]
    chans = api.call("GET", f"/guilds/{gid}/channels")
    live = next(c for c in chans if c["name"] == "live-sessions" and c["type"] == 0)
    forum = next((c for c in chans if c["name"] == "sessions" and c["type"] == 15), None)
    print(f"\nguild {gid}, live-sessions {live['id']} parent {live['parent_id']}, forum exists: {bool(forum)}")
    if not a.apply and not (a.rebuild and forum):
        return
    if not forum:
        forum = api.call("POST", f"/guilds/{gid}/channels", {
            "name": "sessions", "type": 15, "topic": TOPIC, "parent_id": live["parent_id"],
            "available_tags": [{"name": t} for t in TAGS]})
        sibs = sorted([c for c in api.call("GET", f"/guilds/{gid}/channels")
                       if c.get("parent_id") == live["parent_id"] and c["id"] != forum["id"]],
                      key=lambda c: c["position"])
        order = []
        for c in sibs:
            order.append(c["id"])
            if c["id"] == live["id"]: order.append(forum["id"])
        api.call("PATCH", f"/guilds/{gid}/channels",
                 [{"id": cid, "position": i} for i, cid in enumerate(order)])
        print("created forum", forum["id"])
    fid = forum["id"]
    forum = api.call("GET", f"/channels/{fid}")
    have = {t["name"] for t in forum.get("available_tags", [])}
    if set(TAGS) - have:
        tags = [{"id": t["id"]} for t in forum["available_tags"]] + [{"name": t} for t in TAGS if t not in have]
        forum = api.call("PATCH", f"/channels/{fid}", {"available_tags": tags})
    tid = {t["name"]: t["id"] for t in forum["available_tags"]}
    threads = {t["id"]: t["name"] for t in api.call("GET", f"/guilds/{gid}/threads/active")["threads"]
               if t["parent_id"] == fid}
    before = ""
    while True:
        q = f"/channels/{fid}/threads/archived/public?limit=100" + (f"&before={before}" if before else "")
        res = api.call("GET", q)
        threads.update({t["id"]: t["name"] for t in res["threads"]})
        if not res.get("has_more") or not res["threads"]: break
        before = res["threads"][-1]["thread_metadata"]["archive_timestamp"]
    if a.rebuild:
        return rebuild(api, fid, rows, tid, threads, a.apply)
    strip = lambda n: re.sub(r"^#\d+ · [^·]+ · ", "", n)
    by_title = {strip(n): (i, n) for i, n in threads.items()}
    made = 0
    for r in rows:
        hit = by_title.get(strip(r["_t"]))
        if hit:
            tid_, cur = hit
            if cur != r["_t"]:
                api.call("PATCH", f"/channels/{tid_}", {"name": r["_t"]}); time.sleep(1)
                print("renamed", cur, "->", r["_t"])
            # starter message id == thread id
            api.call("PATCH", f"/channels/{tid_}/messages/{tid_}",
                     {"content": r["_b"], "allowed_mentions": {"parse": []}}); time.sleep(1)
            continue
        api.call("POST", f"/channels/{fid}/threads", {
            "name": r["_t"], "applied_tags": [tid[t] for t in r["_tags"]],
            "message": {"content": r["_b"], "allowed_mentions": {"parse": []}}})
        made += 1; print("created", r["_t"]); time.sleep(1.5)
    print("created", made)
    msg = (f"Each session now has its own post in <#{fid}>. "
           "Head there to discuss a specific talk.")
    recent = api.call("GET", f"/channels/{live['id']}/messages?limit=100")
    if any(f"<#{fid}>" in m["content"] for m in recent):
        print("live-sessions pointer already posted")
    else:
        api.call("POST", f"/channels/{live['id']}/messages",
                 {"content": msg, "allowed_mentions": {"parse": []}})
        print("posted pointer")


if __name__ == "__main__":
    main()
