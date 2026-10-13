#!/usr/bin/env python3
"""Build Thursday table tent cards (HTML + PDF) from docs/final/seating_*.json
and scripts/table_starters.json. Deterministic; rerun after the seating JSON changes.

Usage: python3 scripts/build_table_cards.py [docs/final/seating_2026-10-13.json]
Output: docs/final/table_cards_<date>.html and .pdf next to the seating JSON.
Format: US Letter landscape, one tent card per page. The top half is rotated 180
degrees so it reads from both sides once folded along the middle.
Loud failure: any table over 10, a missing starter, a totals mismatch, or text
overflow in a card aborts with a FAILED banner and no PDF is left behind.
"""
import glob, html, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAXSEATS = 10
MEAL_LABEL = {"Thursday lunch": "Thursday lunch", "Thursday dinner": "Thursday dinner"}
# Dinner tables with no listed order in the JSON are sorted lunch first.
MEALS = ["Thursday lunch", "Thursday dinner"]

def fail(msg):
    print("\n" + "=" * 70 + "\nFAILED: " + msg + "\n" + "=" * 70, file=sys.stderr)
    sys.exit(1)

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(ROOT, "docs/final/seating_*.json")))[-1]
    data = json.load(open(src))
    date = re.search(r"seating_(.+)\.json", os.path.basename(src)).group(1)
    outdir = os.path.dirname(os.path.abspath(src))
    starters = {(e["meal"], int(e["table"])): e["questions"] for e in json.load(open(os.path.join(ROOT, "scripts/table_starters.json")))}
    # contract checks
    for k in ("generated_at", "source_files", "db_counts", "meals"):
        if k not in data: fail(f"seating json missing key {k!r}")
    cards, problems = [], []
    totals = {}
    for meal in MEALS:
        if meal not in data["meals"]: fail(f"meal {meal!r} missing from seating json")
        tabs = data["meals"][meal]
        totals[meal] = sum(len(t["seated"]) for t in tabs.values())
        for tid in sorted(tabs, key=int):
            t = tabs[tid]
            if len(t["seated"]) > MAXSEATS: problems.append(f"{meal} table {tid} has {len(t['seated'])} (>10)")
            q = starters.get((meal, int(tid)))
            if not q or len(q) != 4 or not all(x.strip() for x in q): problems.append(f"{meal} table {tid}: need 4 starters, got {q!r}")
            if not t.get("theme"): problems.append(f"{meal} table {tid}: no theme")
            cards.append((meal, int(tid), t, q))
    thu = data["db_counts"]["thu"]
    # Dinner may be lower than DB count only by the lunch-only people recorded in the json.
    # Extras (guests not in the DB, same contract as young_final_tally.py) add to the meals they list.
    extras = data.get("extras", []) or []
    x_lunch = sum("Thursday lunch" in x.get("meals", []) for x in extras)
    x_dinner = sum("Thursday dinner" in x.get("meals", []) for x in extras)
    if totals["Thursday lunch"] != thu + x_lunch: problems.append(f"lunch seated {totals['Thursday lunch']} != DB Thursday count {thu} + {x_lunch} extras")
    lunch_only = data.get("lunch_only_expected", 3)
    if totals["Thursday dinner"] != thu - lunch_only + x_dinner: problems.append(f"dinner seated {totals['Thursday dinner']} != DB {thu} minus {lunch_only} lunch-only + {x_dinner} extras")
    if problems: fail("; ".join(problems))

    e = html.escape
    def half(meal, tid, t, q, flip):
        names = "".join(f'<li{" class=pn" if p["panelist"] else ""}>{e(p["name"])}{"<sup>&#9670;</sup>" if p["panelist"] else ""}</li>' for p in t["seated"])
        qs = "".join(f"<li>{e(x)}</li>" for x in q)
        return f'''<div class="half {'flip' if flip else ''}"><div class="card">
  <aside>
    <div class="logos"><img class="aha" src="../../assets/logos/aha-logo-white.svg" alt=""><img class="lock" src="../../assets/logos/ces-snf-lockup@2x.png" alt=""></div>
    <div class="num"><span class="tl">Table</span><span class="n">{tid}</span></div>
    <div class="meal">{e(meal)}<br><span>October 15, 2026</span></div>
  </aside>
  <main>
    <h2>{e(t["theme"])}</h2><hr>
    <h3>At this table <b class=lg>&#9670; presenting</b></h3><ul class="names">{names}</ul>
    <h3>Conversation starters</h3><ol class="qs">{qs}</ol>
  </main></div></div>'''
    pages = "\n".join(f'<section class="page" data-card="{e(m)} {tid}">{half(m,tid,t,q,True)}<div class="fold"></div>{half(m,tid,t,q,False)}</section>' for m, tid, t, q in cards)
    css = open(os.path.join(ROOT, "docs/final/fonts/fonts.css")).read().replace("url(", "url(fonts/")
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>Table cards {date}</title><style>
{css}
@page {{ size: 11in 8.5in; margin: 0 }}
:root {{ --navy:#002D72; --gold:#FFD100; --text:#1D1D1B; --gray:#5b6577 }}
* {{ box-sizing:border-box; margin:0; padding:0 }}
html,body {{ background:#fff; -webkit-print-color-adjust:exact; print-color-adjust:exact }}
body {{ font-family:'DM Sans',sans-serif; color:var(--text) }}
.page {{ width:11in; height:8.5in; position:relative; overflow:hidden; page-break-after:always; break-after:page }}
.half {{ position:absolute; left:0; width:11in; height:4.25in }}
.half:first-child {{ top:0 }} .half.flip {{ transform:rotate(180deg) }}
.half:not(.flip) {{ top:4.25in }}
.fold {{ position:absolute; left:0; right:0; top:4.25in; border-top:1px dashed #b9bfcc }}
.card {{ position:absolute; inset:0.17in 0.3in; display:flex; border:1.5px solid var(--navy); border-radius:6px; overflow:hidden; background:#fff }}
aside {{ width:2.6in; background:var(--navy); color:#fff; padding:0.16in 0.2in 0.14in; display:flex; flex-direction:column; justify-content:space-between; align-items:center; text-align:center }}
.logos {{ display:flex; flex-direction:column; align-items:center; gap:0.09in }}
.aha {{ height:0.24in; opacity:.95 }} .lock {{ width:1.95in }}
.num {{ display:flex; flex-direction:column; align-items:center; line-height:1 }}
.tl {{ font-size:11pt; letter-spacing:.32em; text-transform:uppercase; color:var(--gold); font-weight:500; margin-bottom:.06in; padding-left:.32em }}
.n {{ font-family:'Cormorant Garamond',serif; font-weight:700; font-size:84pt; font-variant-numeric:lining-nums; color:#fff; line-height:.9 }}
.meal {{ font-family:'Cormorant Garamond',serif; font-size:15pt; font-weight:600; border-top:2px solid var(--gold); padding-top:.08in; width:100% }}
.meal span {{ font-family:'DM Sans',sans-serif; font-size:9pt; letter-spacing:.08em; text-transform:uppercase; font-weight:400; opacity:.85 }}
main {{ flex:1; padding:0.14in 0.3in 0.1in; display:flex; flex-direction:column; min-width:0 }}
.kick {{ font-size:7.5pt; letter-spacing:.2em; text-transform:uppercase; color:var(--gray); font-weight:500 }}
h2 {{ font-family:'Cormorant Garamond',serif; font-weight:700; font-size:21pt; color:var(--navy); line-height:1.05; margin-top:.02in }}
hr {{ border:0; height:3px; width:.7in; background:var(--gold); margin:.06in 0 .04in }}
h3 {{ font-size:7pt; letter-spacing:.2em; text-transform:uppercase; color:var(--navy); font-weight:700; margin:.07in 0 .04in }}
.lg {{ font-weight:400; letter-spacing:.06em; text-transform:none; color:var(--gray); margin-left:.1in; font-size:6.5pt }}
.names {{ list-style:none; columns:2; column-gap:.3in; font-size:10pt; line-height:1.32; font-weight:500 }}
.names li {{ break-inside:avoid; white-space:nowrap }}
.names sup {{ color:#c9a000; font-size:6pt; margin-left:3px; vertical-align:super }}
.qs {{ font-family:'Cormorant Garamond',serif; font-size:11pt; line-height:1.17; font-variant-numeric:lining-nums; color:var(--text); padding-left:.2in; font-weight:500 }}
.qs li {{ margin-bottom:.03in; padding-left:.04in }}
.qs li::marker {{ color:var(--navy); font-weight:700 }}
</style></head><body>
{pages}
<script>
// overflow detector: any card whose content spills is reported in the title
window.addEventListener('load',()=>{{ let bad=[];
 document.querySelectorAll('.card').forEach(c=>{{ const r=c.getBoundingClientRect();
  c.querySelectorAll('main *, aside *').forEach(x=>{{ const b=x.getBoundingClientRect(); const cr=c.getBoundingClientRect();
   }});
  const m=c.querySelector('main'), a=c.querySelector('aside');
  [m,a].forEach(el=>{{ if(el.scrollHeight>el.clientHeight+1||el.scrollWidth>el.clientWidth+1) bad.push(c.closest('.page').dataset.card); }});
 }});
 document.title = bad.length ? 'OVERFLOW:'+[...new Set(bad)].join('|') : 'OK';
}});
</script></body></html>'''
    hp = os.path.join(outdir, f"table_cards_{date}.html"); pp = os.path.join(outdir, f"table_cards_{date}.pdf")
    open(hp, "w").write(page)
    if not os.path.exists(CHROME): fail("Chrome not found at " + CHROME)
    dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", "file://" + hp], capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"<title>(.*?)</title>", dom)
    if os.environ.get("CARDS_DEBUG") and m and m.group(1) != "OK": print("DEBUG (not for print):", m.group(1)[:200])
    elif not m or m.group(1) != "OK": fail("card overflow or check did not run: " + (m.group(1) if m else "no title"))
    if os.path.exists(pp): os.remove(pp)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=5000", f"--print-to-pdf={pp}", "file://" + hp], capture_output=True, timeout=180)
    if not os.path.exists(pp): fail("PDF not produced")
    print(f"OK: {len(cards)} cards -> {pp}\n    html -> {hp}")

if __name__ == "__main__":
    main()
