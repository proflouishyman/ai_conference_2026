import sqlite3, collections
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

NOEAT={'greg hager','mark spindler','anjalie field'}
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

# ---- 3. Meal 1 seating
SESSION_TOOL={4:"GIS/mapping",16:"GIS/mapping",23:"GIS/mapping",20:"OCR",24:"OCR",3:"OCR",
 13:"Networks",21:"Databases",9:"Databases",17:"Archives",11:"ML/CV",5:"Databases",19:"Databases",
 22:"Pedagogy",12:"Pedagogy",6:"Pedagogy",18:"Pedagogy",7:"LLMs",10:"LLMs",14:"LLMs",
 25:"Text mining/NLP",28:"Environment",2:"Plenary",8:"Plenary",15:"Plenary"}
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
NT1=-(-len([r for r in rows if r[9]==1])//9)  # target ~9/table so there is slack
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
    cand=[t for t in tables if len(t["p"])<2]
    if not cand: break
    max(cand, key=lambda t: -len(t["p"]))["p"].append(p)

ws3=wb.create_sheet("Meal 1 - Lunch 10-15")
ws3.append(["Table","Theme","Seat","Name","Institution","Role","Panelist","Dietary"])
pan_emails={p[2].lower() for t in tables for p in t["p"]}
# attendees by best-matching theme -- BALANCED fill
attend=[r for r in rows if r[9]==1 and (not is_panelist(r[3]) or "hyman" in r[3].lower())]
cap=10
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

for t in tables:
    seat=0
    for nm,th,em in t["p"]:
        seat+=1
        rec=next((r for r in rows if r[3].lower()==em.lower()), None)
        inst=rec[4] if rec else ""; role=rec[5] if rec else ""
        d=(rec[6].strip() if rec else ""); d="" if d.lower() in DIETNULL else d
        ws3.append([t["id"],t["theme"],seat,nm,inst,role,"PANELIST",d])
    for r in assigned[t["id"]]:
        seat+=1
        d=r[6].strip(); d="" if d.lower() in DIETNULL else d
        ws3.append([t["id"],t["theme"],seat,f"{r[1]} {r[2]}",r[4],r[5],"",d])
    while seat<cap:
        seat+=1
        ws3.append([t["id"],t["theme"],seat,"","","","",""])
style(ws3,[7,16,6,26,34,22,10,34])

ws4=wb.create_sheet("Meal 2 - Dinner 10-15")
ws4.append(["Table","Grouping","Seat","Name","Institution","Role","Panelist","Dietary"])
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
diners=[r for r in rows if r[9]==1]
byid={r[0]:r for r in diners}
NT=18; CAP=10; PCAP=2
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
    if len(hi[1])-len(lo[1])<=1 or len(hi[1])<=CAP and len(lo[1])>=CAP-2: break
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

tables.sort(key=lambda t:(t[0],-len(t[1])))
for tid,(g,mem) in enumerate(tables,1):
    for i,rid in enumerate(mem,1):
        r=byid[rid]; d=r[6].strip(); d="" if d.lower() in DIETNULL else d
        ws4.append([tid,g,i,f"{r[1]} {r[2]}",r[4],r[5],"PANELIST" if is_panelist(r[3]) else "",d])
    for i in range(len(mem)+1,CAP+1): ws4.append([tid,g,i,"","","","",""])
style(ws4,[7,32,6,26,34,22,10,34])
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
ws6.append(["Tables","18"]); ws6.append(["Seats per table","10"]); ws6.append(["Total capacity","180"])
ws6.append(["Panelists at meals",str(len(plist))])
ws6.append(["Panelists not eating","Greg Hager, Mark Spindler, Anjalie Field"])
ws6.append(["Not at meals (remote)","Mark Humphries"])
ws6.append(["Dietary restrictions",str(DIET_N)])
ws6.column_dimensions['A'].width=30; ws6.column_dimensions['B'].width=44
for c in ws6['A']: c.font=Font(bold=True)

wb.save("conference_seating.xlsx")
print("written: conference_seating.xlsx")
print("sheets:", wb.sheetnames)
