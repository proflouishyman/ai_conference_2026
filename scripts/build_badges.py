#!/usr/bin/env python3
"""Build print-ready 4x3in conference badges from the CURRENT final seating (read-only).

Usage: python3 scripts/build_badges.py [docs/final/seating_2026-10-13.json]
Output (next to the seating json): badges_<date>.pdf / .html / .csv
PDF: one 4x3in page per side, ordered front, back, front, back ... sorted by last then first name.
PRIVATE: outputs contain attendee names. docs/ is gitignored; do not commit.

Data contract
  Input seating json: meals["Thursday lunch"|"Thursday dinner"][table]["seated"] = [{name,email,panelist}],
        extras = [{name, role, meals}] guests not in the DB (institution from EXTRA_INST below; unmapped = FAIL).
  DB (read-only): view effective_attendance (holds_seat=1) JOIN registrations_corrected, table alternate_emails.
  Name/institution cleanup: badge_name() / badge_inst() extracted by ast from build_catering_workbook.py
        (never imported or run as a program).
  Table match order: email, alternate_emails (either direction), exact normalized full name.
  ASL interpreters (email @VENDOR) are keyed by email+name and printed with institution "ASL Interpreter".
  CSV columns: first,last,institution,lunch_table,dinner_table,source(db|extra|asl)
Loud failure: unmatched Thursday eater, duplicate badge, unseated seated-person, text that cannot fit,
  Chrome failure, count mismatch -> FAILED banner, non-zero exit, no PDF left behind.
"""
import ast, csv, glob, html, json, os, re, sqlite3, subprocess, sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LUNCH, DINNER = "Thursday lunch", "Thursday dinner"
EXTRA_INST = None  # name -> institution for seating extras; loaded from private_overrides.json["badge_extra_inst"] (names stay out of git)
NAVY = "#1f3a5f"

problems = []
def banner_fail():
    print("\n" + "=" * 70 + "\nFAILED: %d problem(s)" % len(problems), file=sys.stderr)
    for p in problems: print("  - " + p, file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    sys.exit(1)

def load_badge_fns():
    """Extract PRIV, NAME_FIX, VENDOR, badge_name, INST_FIX, badge_inst by AST (no build is run)."""
    src = open(os.path.join(ROOT, "scripts/build_catering_workbook.py")).read()
    want = {"PRIV", "NAME_FIX", "INST_FIX", "VENDOR"}; funcs = {"badge_name", "badge_inst"}
    keep = [n for n in ast.parse(src).body
            if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in want for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in funcs)]
    got = {t.id for n in keep if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    gf = {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    if got != want or gf != funcs:
        raise SystemExit(f"FAILED: could not extract badge helpers (got {sorted(got)}, {sorted(gf)})")
    ns = {"re": re, "json": json}
    cwd = os.getcwd(); os.chdir(ROOT)  # PRIV path is relative to repo root
    try: exec(compile(ast.Module(body=keep, type_ignores=[]), "build_catering_workbook(badges)", "exec"), ns)
    finally: os.chdir(cwd)
    global EXTRA_INST
    EXTRA_INST = ns["PRIV"].get("badge_extra_inst")
    if not isinstance(EXTRA_INST, dict):
        raise SystemExit("FAILED: private_overrides.json has no badge_extra_inst mapping {name: institution}")
    return ns["badge_name"], ns["badge_inst"], ns["VENDOR"]

def nn(s): return " ".join((s or "").lower().split())

def main():
    badge_name, badge_inst, VENDOR = load_badge_fns()
    src = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(ROOT, "docs/final/seating_*.json")))[-1]
    data = json.load(open(src))
    date = re.search(r"seating_(.+)\.json", os.path.basename(src)).group(1)
    outdir = os.path.dirname(os.path.abspath(src))
    for k in ("meals", "extras"):
        if k not in data: problems.append(f"seating json missing key {k!r}")
    for m in (LUNCH, DINNER):
        if m not in data.get("meals", {}): problems.append(f"meal {m!r} missing")
    if problems: banner_fail()

    # per-meal lookups: key -> table. ASL keyed email|name. Track which seated entries get used.
    seat = {}
    for meal in (LUNCH, DINNER):
        by_email, by_name, entries = {}, {}, []
        for tid, t in data["meals"][meal].items():
            for p in t["seated"]:
                e = (p.get("email") or "").strip().lower()
                key = e + ("|" + nn(p["name"]) if VENDOR in e else "")
                ent = {"table": int(tid), "name": p["name"], "key": key, "used": False}
                entries.append(ent)
                if e:
                    if key in by_email: problems.append(f"{meal}: duplicate seat key {key}")
                    by_email[key] = ent
                if nn(p["name"]) in by_name: by_name[nn(p["name"])] = None  # ambiguous name
                else: by_name[nn(p["name"])] = ent
        seat[meal] = (by_email, by_name, entries)

    con = sqlite3.connect(os.path.join(ROOT, "registrations.db"))
    alt = {}
    for a, c in con.execute("SELECT alt_email, canonical_email FROM alternate_emails"):
        a, c = a.strip().lower(), c.strip().lower()
        alt.setdefault(a, set()).add(c); alt.setdefault(c, set()).add(a)
    rows = con.execute("""SELECT e.first_name, e.last_name, e.email, e.institution, e.eats_oct15, e.eats_oct16
        FROM effective_attendance e JOIN registrations_corrected r USING(response_id) WHERE e.holds_seat=1""").fetchall()
    n_hold = con.execute("SELECT count(*) FROM effective_attendance WHERE holds_seat=1").fetchone()[0]
    if len(rows) != n_hold: problems.append(f"join dropped rows: {len(rows)} joined vs {n_hold} holds_seat")

    def find(meal, email, names):
        by_email, by_name, _ = seat[meal]
        suffix = ("|" + nn(names[0])) if VENDOR in email else ""
        for cand in [email] + sorted(alt.get(email, ())):
            ent = by_email.get(cand + (("|" + nn(names[0])) if VENDOR in cand else ""))
            if ent: return ent
        for n in names:
            ent = by_name.get(nn(n))
            if ent: return ent
        return None

    badges = []
    for fn, ln, email, inst, d15, d16 in rows:
        email = (email or "").strip().lower()
        first, last = badge_name(fn, ln, email)
        asl = email.endswith("@" + VENDOR)
        names = [f"{first} {last}", f"{fn} {ln}"]
        tabs = {}
        for meal in (LUNCH, DINNER):
            ent = find(meal, email, names)
            if ent: ent["used"] = True
            tabs[meal] = ent["table"] if ent else ""
        if d15 and not tabs[LUNCH]:
            problems.append(f"Thursday eater not at any lunch table: {first} {last} <{email}>")
        badges.append(dict(first=first, last=last, inst="ASL Interpreter" if asl else badge_inst(inst),
                           lunch=tabs[LUNCH], dinner=tabs[DINNER], source="asl" if asl else "db",
                           d15=bool(d15), email=email))
    for x in data["extras"]:
        parts = x["name"].split(" ", 1)
        if x["name"] not in EXTRA_INST or len(parts) != 2:
            problems.append(f"extra {x['name']!r}: no institution mapping or not 'First Last'"); continue
        tabs = {}
        for meal in (LUNCH, DINNER):
            ent = seat[meal][1].get(nn(x["name"]))
            if ent: ent["used"] = True
            tabs[meal] = ent["table"] if ent else ""
            if meal in x.get("meals", []) and not ent: problems.append(f"extra {x['name']} not seated at {meal}")
        badges.append(dict(first=parts[0], last=parts[1], inst=EXTRA_INST[x["name"]], lunch=tabs[LUNCH],
                           dinner=tabs[DINNER], source="extra", d15=True, email=""))
    # every seated person must belong to a badge
    for meal in (LUNCH, DINNER):
        for ent in seat[meal][2]:
            if not ent["used"]: problems.append(f"{meal} table {ent['table']}: seated {ent['name']!r} matches no badge holder")
    # duplicates
    seen = Counter((nn(b["first"]), nn(b["last"]), nn(b["inst"])) for b in badges)
    for k, v in seen.items():
        if v > 1: problems.append(f"duplicate badge x{v}: {k}")
    for b in badges:
        if not b["first"].strip() or not b["last"].strip(): problems.append(f"unmapped/empty name: {b['first']!r} {b['last']!r} <{b['email']}>")
    expected = n_hold + len(data["extras"])
    if len(badges) != expected: problems.append(f"badge count {len(badges)} != expected {expected}")
    lunch_only = [b for b in badges if b["d15"] and b["lunch"] and not b["dinner"]]
    if len(lunch_only) != data.get("lunch_only_expected", len(lunch_only)):
        problems.append(f"lunch-only count {len(lunch_only)} != lunch_only_expected {data.get('lunch_only_expected')}")
    if problems: banner_fail()

    blank_inst = [f"{b['first']} {b['last']}" for b in badges if not b['inst'].strip()]
    badges.sort(key=lambda b: (b["last"].lower(), b["first"].lower()))
    e = html.escape
    def face(b, back):
        foot = ""
        if back:
            l = f'<span>Thursday lunch: <b>Table {b["lunch"]}</b></span>' if b["lunch"] else "<span></span>"
            d = f'<span>Thursday dinner: <b>Table {b["dinner"]}</b></span>' if b["dinner"] else "<span></span>"
            foot = f'<div class="foot">{l}{d}</div>'
        return (f'<section class="page" data-id="{e(b["first"])} {e(b["last"])} {"back" if back else "front"}">'
                f'<div class="slot"></div><div class="mid"><div class="fn fit">{e(b["first"])}</div>'
                f'<div class="ln fit">{e(b["last"])}</div><div class="inst fit">{e(b["inst"])}</div></div>{foot}</section>')
    pages = "\n".join(face(b, False) + "\n" + face(b, True) for b in badges)
    css = open(os.path.join(ROOT, "docs/final/fonts/fonts.css")).read().replace("url(", "url(fonts/")
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>Badges {date}</title><style>
{css}
@page {{ size: 4in 3in; margin: 0 }}
* {{ box-sizing:border-box; margin:0; padding:0 }}
html,body {{ background:#fff; -webkit-print-color-adjust:exact; print-color-adjust:exact }}
body {{ font-family:'DM Sans',sans-serif; color:#3a3a3a }}
.page {{ width:4in; height:3in; position:relative; overflow:hidden; page-break-after:always; break-after:page; background:#fff }}
.slot {{ position:absolute; top:.17in; left:50%; width:.55in; height:.17in; margin-left:-.275in; border:1px dashed #b5b5b5; border-radius:.09in }}
.mid {{ position:absolute; top:.5in; left:.2in; right:.2in; height:1.85in; text-align:center }}
.fn,.ln {{ font-family:'Cormorant Garamond',serif; font-weight:700; color:{NAVY}; line-height:1.05; white-space:nowrap }}
.fn {{ font-size:60pt; height:.95in; display:flex; align-items:flex-end; justify-content:center }}
.ln {{ font-size:42pt; height:.65in; display:flex; align-items:center; justify-content:center }}
.inst {{ font-size:13.5pt; color:#3a3a3a; line-height:1.15; height:.5in; display:flex; align-items:flex-start; justify-content:center; text-align:center; margin-top:.05in }}
.foot {{ position:absolute; left:.2in; right:.2in; bottom:.17in; display:flex; justify-content:space-between; font-size:8pt; color:#3a3a3a }}
.foot b {{ color:{NAVY}; font-weight:700 }}
</style></head><body>
{pages}
<script>
// Fit: shrink font until the text fits its box (inner span measured). Report shrunk / failed in the title.
window.addEventListener('load',()=>{{ const shrunk=[], bad=[];
 document.querySelectorAll('.fit').forEach(el=>{{
  const txt=el.textContent; const maxW=el.parentElement.clientWidth; const id=el.closest('.page').dataset.id;
  const isBack=id.endsWith('back');
  const base=parseFloat(getComputedStyle(el).fontSize); const isInst=el.classList.contains('inst');
  const s=document.createElement('span'); s.textContent=txt; s.style.whiteSpace='nowrap'; el.textContent=''; el.appendChild(s);
  let fs=base, min=isInst?14.67:(el.classList.contains('fn')?26:20);
  const fits=()=>s.getBoundingClientRect().width<=maxW-2;
  while(!fits() && fs>min){{ fs-=0.5; el.style.fontSize=fs+'px'; }}
  let wrapped=false;
  if(!fits() && isInst){{ // two-line wrap for long institutions
    s.style.whiteSpace='normal'; s.style.display='block'; s.style.width=maxW+'px'; wrapped=true; fs=base; el.style.fontSize=fs+'px';
    while((s.getBoundingClientRect().height>el.clientHeight+1 || s.scrollWidth>maxW+1) && fs>9){{ fs-=0.5; el.style.fontSize=fs+'px'; }}
    if(s.getBoundingClientRect().height>el.clientHeight+1 || s.scrollWidth>maxW+1) bad.push(id+' '+txt);
  }} else if(!fits()) bad.push(id+' '+txt);
  if(!isBack && (fs<base||wrapped)) shrunk.push(id.replace(/ front$/,'')+' | '+txt+' | '+(base*0.75).toFixed(1)+'pt -> '+(fs*0.75).toFixed(1)+'pt'+(wrapped?' (wrapped)':''));
 }});
 document.title='RESULT:'+JSON.stringify({{shrunk,bad}});
}});
</script></body></html>'''
    hp = os.path.join(outdir, f"badges_{date}.html"); pp = os.path.join(outdir, f"badges_{date}.pdf")
    cp = os.path.join(outdir, f"badges_{date}.csv")
    open(hp, "w").write(page)
    if os.path.exists(pp): os.remove(pp)
    if not os.path.exists(CHROME): problems.append("Chrome not found at " + CHROME); banner_fail()
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", "file://" + hp],
                       capture_output=True, text=True, timeout=300)
    m = re.search(r"<title>RESULT:(.*?)</title>", r.stdout, re.S)
    if not m: problems.append("fit check did not run (no RESULT title); Chrome rc=%s %s" % (r.returncode, r.stderr[-300:])); banner_fail()
    res = json.loads(html.unescape(m.group(1)))
    if res["bad"]: problems.extend("cannot fit on badge: " + x for x in res["bad"]); banner_fail()
    # Bake the measured sizes into the saved html: use the DOM Chrome produced, minus the script.
    dom = r.stdout
    dom = re.sub(r"<script>.*?</script>", "", dom, flags=re.S)
    dom = re.sub(r"<title>RESULT:.*?</title>", f"<title>Badges {date}</title>", dom, flags=re.S)
    open(hp, "w").write(dom)
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=5000",
                        f"--print-to-pdf={pp}", "file://" + hp], capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(pp):
        problems.append(f"PDF not produced (rc={r.returncode}) {r.stderr[-300:]}"); banner_fail()
    with open(cp, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["first", "last", "institution", "lunch_table", "dinner_table", "source"])
        for b in badges: w.writerow([b["first"], b["last"], b["inst"], b["lunch"], b["dinner"], b["source"]])
    nl = sum(1 for b in badges if b["lunch"]); nd = sum(1 for b in badges if b["dinner"])
    nt = sum(1 for b in badges if not b["lunch"] and not b["dinner"])
    print(f"OK badges={len(badges)} (expected {n_hold} holds_seat + {len(data['extras'])} extras = {expected})")
    print(f"  with lunch table {nl}, with dinner table {nd}, no table at all {nt}, lunch-only {len(lunch_only)}")
    print(f"  extras {sum(b['source']=='extra' for b in badges)}, ASL {sum(b['source']=='asl' for b in badges)}")
    print(f"  shrunk/wrapped text ({len(res['shrunk'])}):")
    for s in res["shrunk"]: print("    " + s)
    print(f"  BLANK institution (form answer was not an institution, per badge_inst) {len(blank_inst)}: {', '.join(blank_inst)}")
    print(f"  pdf {pp}\n  html {hp}\n  csv {cp}")

if __name__ == "__main__":
    main()
