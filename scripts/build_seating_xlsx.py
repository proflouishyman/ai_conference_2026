import sqlite3, collections, json
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

db=sqlite3.connect('registrations.db')
NAV="1F3A5F"; GOLD="C9A227"
hdr=Font(bold=True,color="FFFFFF"); fill=PatternFill("solid",fgColor=NAV)
def style(ws,widths):
    for c in ws[1]: c.font=hdr; c.fill=fill; c.alignment=Alignment(vertical="center")
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    ws.freeze_panes="A2"

TOOLS=[("Large language models","LLMs"),("Text mining","Text mining/NLP"),
 ("Geographic Information Systems","GIS/mapping"),("Network analysis","Networks"),
 ("Data visualization","Dataviz"),("Machine learning","ML/CV"),
 ("Optical character recognition","OCR"),("Database construction","Databases")]
def tools_of(t):
    t=(t or "").strip()
    if not t or t=="None of the above": return []
    return [s for k,s in TOOLS if k in t]

rows=db.execute("""SELECT ea.response_id, ea.first_name, ea.last_name, ea.email, ea.institution,
  COALESCE(r.role,''), COALESCE(r.dietary_restrictions,''), COALESCE(r.tools_used,''),
  COALESCE(r.experience_level,''), ea.eats_oct15, ea.eats_oct16, COALESCE(ea.extent,'no_reply')
 FROM effective_attendance ea JOIN registrations_corrected r USING(response_id)
 ORDER BY ea.last_name, ea.first_name""").fetchall()

pan={}
for em,nm,ss in db.execute("SELECT email, full_name, sessions FROM panelists"):
    pan[em.lower()]=(nm,ss)
alt={}
try:
    for a,c in db.execute("SELECT lower(alt_email), lower(canonical_email) FROM alternate_emails"): alt[a]=c
except Exception: pass
def is_panelist(email):
    e=email.lower()
    return e in pan or alt.get(e,"") in pan
def sessions_of(email):
    e=email.lower(); e=alt.get(e,e)
    return pan.get(e,("",""))[1]

# Personal overrides (names, emails) live in a gitignored file.
PRIV=json.load(open('scripts/private_overrides.json'))
NOEAT=set(PRIV['panelists_not_eating'])
wb=Workbook()

# ---- 1. Attendees
ws=wb.active; ws.title="Attendees"
ws.append(["Last","First","Email","Institution","Role","Panelist","Sessions",
           "Dietary restriction","Experience","Technical interests","Eats 10/15","Eats 10/16","Survey"])
DIETNULL={'no','none','n/a','na','nope','no.','-','none.','no restrictions','','n/a.'}
for rid,fn,ln,em,inst,role,diet,tools,exp,e15,e16,ext in rows:
    d=diet.strip(); d="" if d.lower() in DIETNULL else d
    ws.append([ln,fn,em,inst,role,"YES" if is_panelist(em) else "",sessions_of(em),d,
               exp,", ".join(tools_of(tools)),"Y" if e15 else "N","Y" if e16 else "N",
               "" if ext=="no_reply" else ext])
style(ws,[18,15,32,34,22,9,9,40,38,42,9,9,12])

# ---- 2. Dietary only
ws2=wb.create_sheet("Dietary")
ws2.append(["Last","First","Institution","Dietary restriction","Eats 10/15","Eats 10/16"])
n=0
for rid,fn,ln,em,inst,role,diet,tools,exp,e15,e16,ext in rows:
    d=diet.strip()
    if d.lower() in DIETNULL: continue
    n+=1
    ws2.append([ln,fn,inst,d,"Y" if e15 else "N","Y" if e16 else "N"])
DIET_N=n
style(ws2,[18,15,34,52,10,10])


# --- ASL: the interpreters sit with the person they interpret for -----------
# Two agency interpreters attend Thursday for one attendee. They are seated
# at that person's table at lunch (they are booked 9:00-4:30, so not at dinner), and a table that holds them needs three
# extra chairs rather than one. Seating them anywhere else would defeat the
# booking, so this is enforced after the allocator runs rather than left to it.
ASL_IDS = ('MANUAL-ASL-INTERPRETER-1', 'MANUAL-ASL-INTERPRETER-2')
ASL_HOST = PRIV['asl_host_name_contains']

def pull_asl(pool):
    """Take the interpreters out of a diner pool; return (pool, interpreters)."""
    keep, asl = [], []
    for r in pool:
        (asl if r[0] in ASL_IDS else keep).append(r)
    return keep, asl

# ---- 3. Meal 1 seating
SESSION_TOOL={4:"GIS/mapping",18:"GIS/mapping",25:"GIS/mapping",26:"OCR",19:"OCR",3:"OCR",
 15:"Networks",23:"Databases",10:"Databases",7:"Archives",13:"ML/CV",5:"Databases",12:"Databases",
 24:"Pedagogy",14:"Pedagogy",6:"Pedagogy",20:"Pedagogy",8:"LLMs",11:"LLMs",16:"LLMs",
 21:"Text mining/NLP",22:"Environment",2:"Plenary",9:"Plenary",17:"Plenary"}
plist=[]
for em,nm,ss in db.execute("SELECT email, full_name, sessions FROM panelists"):
    l=nm.lower()
    if l in NOEAT or "hyman" in l: continue
    # A panelist may have registered under an alternate address. Match through
    # the alt map, and carry the REGISTERED email forward so the row lookup
    # (institution, role, dietary) downstream resolves.
    rec=next((r for r in rows if r[3].lower()==em.lower()
              or alt.get(r[3].lower(),"")==em.lower()), None)
    if rec is None: continue
    plist.append([nm,SESSION_TOOL.get(int(str(ss).split(',')[0]),"Other"),rec[3]])
SPEC=[p for p in plist if p[1] not in ("Pedagogy","Plenary")]
GEN=[p for p in plist if p[1] in ("Pedagogy","Plenary")]
byt=collections.defaultdict(list)
for p in SPEC: byt[p[1]].append(p)
themes=sorted(byt,key=lambda t:-len(byt[t]))
seat1=[]
NT1=16  # Only 16 tables fit in the main room (venue walk-through 2026-10-01); 16 x 11 = 176 seats
while len(seat1)<NT1 and any(byt.values()):
    for t in themes:
        if byt[t] and len(seat1)<NT1: seat1.append(byt[t].pop(0))
left=[p for t in byt for p in byt[t]]
tables=[{"id":i+1,"theme":seat1[i][1],"p":[seat1[i]]} for i in range(len(seat1))]
while len(tables)<NT1:
    tables.append({"id":len(tables)+1,"theme":"Mixed","p":[]})

# Place every remaining panelist. Prefer an empty table, then a table whose
# theme differs (to spread pedagogy/plenary people around), but fall back to
# any table under the 2-panelist cap rather than dropping anyone.
for p in GEN+left:
    empty=[t for t in tables if not t["p"]]
    if empty:
        t=empty[0]; t["p"].append(p)
        if t["theme"]=="Mixed": t["theme"]=p[1]
        continue
    cand=[t for t in tables if len(t["p"])<2 and t["theme"]!=p[1]] \
         or [t for t in tables if len(t["p"])<2]
    if cand:
        min(cand, key=lambda t: len(t["p"]))["p"].append(p)

placed={p[2].lower() for t in tables for p in t["p"]}
leftover=[p for p in plist if p[2].lower() not in placed]
for p in leftover:
    # With 16 tables there are more panelists than 2-per-table slots, so the
    # overflow goes to whichever table has the fewest panelists rather than being dropped.
    min(tables, key=lambda t: len(t["p"]))["p"].append(p)

ws3=wb.create_sheet("Meal 1 - Lunch 10-15")
ws3.append(["Table","Theme","Seat","Name","Institution","Role","Panelist","Dietary","Email"])
pan_emails={p[2].lower() for t in tables for p in t["p"]}
# attendees by best-matching theme -- BALANCED fill
attend=[r for r in rows if r[9]==1 and (not is_panelist(r[3]) or "hyman" in r[3].lower())]
attend, asl1 = pull_asl(attend)
cap=11
seats={t["id"]:cap-len(t["p"]) for t in tables}
theme_of={t["id"]:t["theme"] for t in tables}
assigned={t["id"]:[] for t in tables}
# rank each attendee's table preferences by their selected tools
def prefs(r):
    ts=tools_of(r[7])
    want=[tid for tid in seats if theme_of[tid] in ts]
    return want
unplaced=[]
# pass 1: everyone who matches a table theme, to the emptiest matching table
for r in sorted(attend, key=lambda r: len(tools_of(r[7])) or 99):
    p=prefs(r)
    p=[t for t in p if seats[t]>0]
    if not p: unplaced.append(r); continue
    t=max(p, key=lambda t: seats[t])
    assigned[t].append(r); seats[t]-=1
# pass 2: everyone else (no tools / no room) into the emptiest tables
for r in unplaced:
    open_t=[t for t in seats if seats[t]>0]
    if not open_t: break
    t=max(open_t, key=lambda t: seats[t])
    assigned[t].append(r); seats[t]-=1

# Seat the interpreters at the ASL host's table, over the normal cap.
_et = next((t["id"] for t in tables
            if any(ASL_HOST in f"{r[1]} {r[2]}".lower() for r in assigned[t["id"]])
            or any(ASL_HOST in nm.lower() for nm,_,_ in t["p"])), None)
if _et is None and tables: _et = tables[0]["id"]
if _et is not None:
    assigned[_et].extend(asl1)
    # Tables are 11 chairs. Make room for the interpreters by moving
    # other attendees from the ASL host's table to the tables with the most space.
    occ=lambda tid: len(assigned[tid])+len(next(t for t in tables if t["id"]==tid)["p"])
    while occ(_et)>cap:
        mv=next((r for r in reversed(assigned[_et]) if r[0] not in ASL_IDS
                 and ASL_HOST not in f"{r[1]} {r[2]}".lower()), None)
        dst=min((t["id"] for t in tables if t["id"]!=_et), key=occ, default=None)
        if mv is None or dst is None or occ(dst)>=cap: break
        assigned[_et].remove(mv); assigned[dst].append(mv)

# Manual lunch swaps, same idea as DINNER_SWAPS below: each pair of emails trades seats.
for e1, e2 in PRIV.get('lunch_swaps', []):
    t1 = next((t for t in assigned for r in assigned[t] if r[3].strip().lower() == e1), None)
    t2 = next((t for t in assigned for r in assigned[t] if r[3].strip().lower() == e2), None)
    if t1 is None or t2 is None or t1 == t2: continue
    r1 = next(r for r in assigned[t1] if r[3].strip().lower() == e1)
    r2 = next(r for r in assigned[t2] if r[3].strip().lower() == e2)
    assigned[t1][assigned[t1].index(r1)] = r2; assigned[t2][assigned[t2].index(r2)] = r1

# The wheelchair user sits at table 1, the table nearest the door: swap table numbers.
DOOR = PRIV['door_seat_name_contains']
_dt = next((t for t in tables if any(DOOR in f"{r[1]} {r[2]}".lower() for r in assigned[t["id"]])), None)
if _dt is not None and _dt["id"] != 1:
    one = next(t for t in tables if t["id"] == 1)
    assigned[1], assigned[_dt["id"]] = assigned[_dt["id"]], assigned[1]
    one["id"], _dt["id"] = _dt["id"], 1
tables.sort(key=lambda t: t["id"])
for t in tables:
    seat=0
    for nm,th,em in t["p"]:
        seat+=1
        rec=next((r for r in rows if r[3].lower()==em.lower()), None)
        inst=rec[4] if rec else ""; role=rec[5] if rec else ""
        d=(rec[6].strip() if rec else ""); d="" if d.lower() in DIETNULL else d
        ws3.append([t["id"],t["theme"],seat,nm,inst,role,"PANELIST",d,rec[3] if rec else em])
    for r in assigned[t["id"]]:
        seat+=1
        d=r[6].strip(); d="" if d.lower() in DIETNULL else d
        ws3.append([t["id"],t["theme"],seat,f"{r[1]} {r[2]}",r[4],r[5],"",d,r[3]])
    while seat<cap:
        seat+=1
        ws3.append([t["id"],t["theme"],seat,"","","","",""])
style(ws3,[7,16,6,26,34,22,10,34,32])

ws4=wb.create_sheet("Meal 2 - Dinner 10-15")
ws4.append(["Table","Grouping","Seat","Name","Institution","Role","Panelist","Dietary","Email"])
sub={}
for rid,era,reg,th in db.execute("SELECT response_id,era,region,theme FROM subfield_classification"):
    sub[rid]=(era,reg,th)
LABEL={"political-diplomatic":"Political & diplomatic",
 "race-slavery-indigenous":"Race, slavery & indigenous",
 "digital-methods-pedagogy":"Digital methods & pedagogy",
 "archives-library-museum":"Archives, libraries & museums",
 "science-tech-medicine":"Science, tech & medicine","environment":"Science, tech & medicine",
 "intellectual-book-history":"Intellectual & book history","religion":"Intellectual & book history",
 "economic-labor":"Economic & labor history","military-war":"Military & war",
 "social-cultural":"Social & cultural","gender-sexuality":"Social & cultural",
 "unknown":"Mixed"}
# Some Thursday diners eat lunch but have said they will skip dinner.
NO_DINNER=set(PRIV.get('no_dinner_emails',[]))
diners=[r for r in rows if r[9]==1 and r[3].strip().lower() not in NO_DINNER]
diners, asl2 = pull_asl(diners)
byid={r[0]:r for r in diners}
NT=16; CAP=11; PCAP=2
# group people by subfield label
groups=collections.defaultdict(list)
for rid in byid:
    groups[LABEL.get(sub.get(rid,("","","unknown"))[2],"Mixed")].append(rid)
# how many tables each subfield deserves, proportional, then largest-remainder
tot=len(byid)
alloc={}; rem=[]
for g,mem in groups.items():
    exact=len(mem)*NT/tot; n=int(exact)
    alloc[g]=n; rem.append((exact-n,g))
while sum(alloc.values())<NT:
    rem.sort(reverse=True); alloc[rem.pop(0)[1]]+=1
for g in list(alloc):
    if alloc[g]==0 and groups[g]: alloc[g]=1
while sum(alloc.values())>NT:
    big=max(alloc, key=lambda g: alloc[g]); alloc[big]-=1
# build tables, splitting each subfield's people evenly and capping panelists
tables=[]
for g,n in alloc.items():
    mem=groups[g]
    pans=[r for r in mem if is_panelist(byid[r][3])]
    others=[r for r in mem if not is_panelist(byid[r][3])]
    buckets=[[] for _ in range(max(n,1))]
    for i,p in enumerate(pans):            # spread panelists round-robin, cap 2
        placed=False
        for k in range(len(buckets)):
            b=buckets[(i+k)%len(buckets)]
            if sum(1 for x in b if is_panelist(byid[x][3]))<PCAP: b.append(p); placed=True; break
        if not placed: others.append(p)
    for i,o in enumerate(others):
        buckets.sort(key=len); buckets[0].append(o)
    for b in buckets: tables.append([g,b])
# even out sizes: move from oversized to undersized tables
tables.sort(key=lambda t: len(t[1]))
while True:
    tables.sort(key=lambda t: len(t[1]))
    lo,hi=tables[0],tables[-1]
    # Even the tables out fully, so spare chairs are spread across tables as slack.
    if len(hi[1])-len(lo[1])<=1: break
    mv=next((x for x in hi[1] if not is_panelist(byid[x][3])), None)
    if mv is None: break
    hi[1].remove(mv); lo[1].append(mv)
# enforce max 2 panelists per table: move surplus panelists to panelist-light tables
def npan(b): return sum(1 for x in b if is_panelist(byid[x][3]))
for _ in range(200):
    over=[t for t in tables if npan(t[1])>PCAP]
    if not over: break
    src=over[0]
    mv=next(x for x in reversed(src[1]) if is_panelist(byid[x][3]))
    dst=min((t for t in tables if t is not src and npan(t[1])<PCAP),
            key=lambda t:(npan(t[1]), len(t[1])), default=None)
    if dst is None: break
    src[1].remove(mv); dst[1].append(mv)
    # keep sizes sane: hand back a non-panelist if dst is now oversized
    if len(dst[1])>CAP:
        back=next((x for x in reversed(dst[1]) if not is_panelist(byid[x][3])), None)
        if back is not None: dst[1].remove(back); src[1].append(back)

# Final even-out after the panelist moves, so the spare chairs are spread as
# slack rather than bunched at a few half-empty tables.
for _ in range(200):
    tables.sort(key=lambda t: len(t[1]))
    lo, hi = tables[0], tables[-1]
    if len(hi[1]) - len(lo[1]) <= 1: break
    mv = next((x for x in reversed(hi[1]) if not is_panelist(byid[x][3])), None)
    if mv is None: break
    hi[1].remove(mv); lo[1].append(mv)

# Seat the interpreters with the ASL host here too, over the cap.
# The interpreting booking ends at 4:30, so the interpreters are not at dinner.
asl2 = []
_host_tbl = next((t for t in tables
                   if any(ASL_HOST in f"{byid[x][1]} {byid[x][2]}".lower() for x in t[1])), None)
if _host_tbl is None and tables: _host_tbl = tables[0]
if _host_tbl is not None:
    _host_tbl[1].extend(r[0] for r in asl2)
    # Same chair limit as lunch: move non-panelists off the ASL host's table.
    while len(_host_tbl[1])>CAP:
        mv=next((x for x in reversed(_host_tbl[1]) if x not in ASL_IDS
                 and not is_panelist(byid[x][3])
                 and ASL_HOST not in f"{byid[x][1]} {byid[x][2]}".lower()), None)
        dst=min((t for t in tables if t is not _host_tbl), key=lambda t: len(t[1]), default=None)
        if mv is None or dst is None or len(dst[1])>=CAP: break
        _host_tbl[1].remove(mv); dst[1].append(mv)

tables.sort(key=lambda t:(t[0],-len(t[1])))
# Manual dinner swaps Louis asked for, applied after allocation so every
# rebuild keeps them. Each pair of emails trades seats.
DINNER_SWAPS = [tuple(x) for x in PRIV['dinner_swaps']]
def _tbl_of(email):
    return next((t for t in tables for x in t[1] if byid[x][3].strip().lower() == email), None)
for e1, e2 in DINNER_SWAPS:
    t1, t2 = _tbl_of(e1), _tbl_of(e2)
    if t1 is None or t2 is None or t1 is t2: continue
    x1 = next(x for x in t1[1] if byid[x][3].strip().lower() == e1)
    x2 = next(x for x in t2[1] if byid[x][3].strip().lower() == e2)
    t1[1][t1[1].index(x1)] = x2; t2[1][t2[1].index(x2)] = x1
_dd = next((t for t in tables if any(DOOR in f"{byid[x][1]} {byid[x][2]}".lower() for x in t[1])), None)
if _dd is not None: tables.remove(_dd); tables.insert(0, _dd)
for tid,(g,mem) in enumerate(tables,1):
    for i,rid in enumerate(mem,1):
        r=byid[rid]; d=r[6].strip(); d="" if d.lower() in DIETNULL else d
        ws4.append([tid,g,i,f"{r[1]} {r[2]}",r[4],r[5],"PANELIST" if is_panelist(r[3]) else "",d,r[3]])
    for i in range(len(mem)+1,CAP+1): ws4.append([tid,g,i,"","","","",""])
style(ws4,[7,32,6,26,34,22,10,34,32])
# Louis asked not to sit with the same people at lunch and dinner. Warn if a rebuild breaks that.
_nr = PRIV.get('no_repeat_tablemates_email')
if _nr:
    def _mates(ws):
        rs = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[3]]
        t = next((r[0] for r in rs if (r[8] or "").strip().lower() == _nr), None)
        return {r[3] for r in rs if r[0] == t and (r[8] or "").strip().lower() != _nr}
    _both = _mates(ws3) & _mates(ws4)
    if _both: print("WARN: same tablemates at lunch and dinner:", sorted(_both), "-- add a lunch_swaps pair")
# ---- 5. Meal 3
ws5=wb.create_sheet("Meal 3 - Lunch 10-16")
ws5.append(["Meal 3, lunch Friday 16 October"])
ws5.append(["OPEN SEATING — no assignments, no place cards needed."])
ws5.append([f"Expected diners: {sum(r[10] for r in rows)}"])
ws5.column_dimensions['A'].width=70
ws5['A1'].font=Font(bold=True,size=12)

# ---- 6. Summary
ws6=wb.create_sheet("Summary")
for k,v in db.execute("SELECT metric, n FROM effective_attendance_summary"): ws6.append([k,v])
ws6.append(["",""])
ws6.append(["Tables",str(NT)]); ws6.append(["Seats per table",str(CAP)]); ws6.append(["Total capacity",str(NT*CAP)])
ws6.append(["Panelists at meals",str(len(plist))])
ws6.append(["Panelists not eating",PRIV["panelists_not_eating_label"]])
ws6.append(["Not at meals (remote)",PRIV["remote_panelists"]])
ws6.append(["Dietary restrictions",str(DIET_N)])
ws6.column_dimensions['A'].width=30; ws6.column_dimensions['B'].width=44
for c in ws6['A']: c.font=Font(bold=True)

wb.save("conference_seating.xlsx")
print("written: conference_seating.xlsx")
print("sheets:", wb.sheetnames)
