"""Build the event planner's catering workbook: dietary counts, attendee list,
Thursday seating and badge data. Run build_seating_xlsx.py first, since the
table assignments are read from conference_seating.xlsx."""
import sqlite3, re, os, json
import openpyxl
from collections import Counter
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = "docs/conference_catering_seating_badges.xlsx"
SEATING = "conference_seating.xlsx"

NONE_RE = re.compile(r"^\s*(none.*|no|n/?a|no restriction)?\s*\.?\s*$", re.I)

# Personal and form-derived values live in a gitignored file.
PRIV = json.load(open("scripts/private_overrides.json"))
# exact (lowercased, stripped) form text -> (category, note)
MAP = {k: tuple(v) for k, v in PRIV["dietary_map"].items()}
MANY = tuple(PRIV["dietary_many"]["value"])
VENDOR = PRIV["asl_vendor_domain"]  # email domain of the ASL interpreter agency

def normalize(text):
    t = (text or "").strip()
    if NONE_RE.match(t) or t.lower() in PRIV.get("dietary_none_extra", []):
        return ("None", "")
    key = t.lower()
    if key.startswith(PRIV["dietary_many"]["prefix"]):
        return MANY
    if key not in MAP:
        raise SystemExit(f"Unmapped dietary value: {t!r}")
    return MAP[key]

# Badge names: what the person goes by, not form typos. Keyed by email.
NAME_FIX = {k: tuple(v) for k, v in PRIV["badge_name_fix"].items()}
def badge_name(fn, ln, email):
    if email in NAME_FIX:
        return NAME_FIX[email]
    out = []
    for x in (fn, ln):
        x = " ".join((x or "").split())
        if x and (x == x.upper() and len(x) > 2 or x == x.lower()):
            x = x.title()
        out.append(x)
    return tuple(out)

# Badge institution: a clean, printable version of what they typed. Blank means
# the form answer was not an institution (a job title, "Student", a surname).
INST_FIX = {
    "JHU": "Johns Hopkins University", "Johns Hopkins": "Johns Hopkins University",
    "johns hopkins history": "Johns Hopkins University", "JHU Museums": "Johns Hopkins University Museums",
    "SNF Agora @ JHU": "SNF Agora Institute", "SNF Agora Institute, Johns Hopkins University": "SNF Agora Institute",
    "Clemson": "Clemson University", "Kent State Univeristy": "Kent State University",
    "University of Balitmore": "University of Baltimore", "Morgan State": "Morgan State University",
    "Princeton": "Princeton University", "Penn": "University of Pennsylvania",
    "Penn GSE": "University of Pennsylvania", "U Maryland": "University of Maryland",
    "The Pine Crest School": "Pine Crest School",
    "US Holocaust Memorial Museum": "United States Holocaust Memorial Museum",
    "Smithsonian": "Smithsonian Institution", "Smithsonian Institution NMAAHC": "Smithsonian NMAAHC",
    "JRP Historical Consulting, LLC": "JRP Historical Consulting",
    "Georgetown University, Department of History": "Georgetown University",
    "Department of State, Office of the Historian": "Office of the Historian, U.S. Department of State",
    "Office of the Historian, US House of Reps": "Office of the Historian, U.S. House of Representatives",
    "University of North Carolina-Chapel Hill": "University of North Carolina at Chapel Hill",
    "Department of War Studies, King's College London": "King's College London",
    "Kluge Center Library of Congress": "Kluge Center, Library of Congress",
    "University of Strathclyde in Glasgow, UK": "University of Strathclyde",
    "Independent": "Independent Scholar", "independent": "Independent Scholar",
    "Non-affiliated Researcher": "Independent Scholar", "Mikaelian": "Independent Scholar",
    "Professor": "Columbia University",
    "NC State University": "North Carolina State University", "U.S. Naval Academy": "United States Naval Academy",
    "Student": "", "Retired": "", "(No formal affiliations)": "",
}
def badge_inst(inst):
    i = " ".join((inst or "").split())
    return INST_FIX.get(i, i)

def load_tables(sheet):
    """email -> table number, plus the sheet rows for the seating tabs."""
    ws = openpyxl.load_workbook(SEATING)[sheet]
    hdr = [c.value for c in ws[1]]
    ti, ni, ei = hdr.index("Table"), hdr.index("Name"), hdr.index("Email")
    rows, by = [], {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r[ni]:
            continue
        rows.append(r)
        key = (r[ei] or "").strip().lower()
        if VENDOR in key:
            key += "|" + r[ni]
        by[key] = r[ti]
    return by, rows
LUNCH, lunch_rows = load_tables("Meal 1 - Lunch 10-15")
DINNER, dinner_rows = load_tables("Meal 2 - Dinner 10-15")

con = sqlite3.connect("registrations.db")
rows = con.execute("""
  SELECT e.first_name, e.last_name, e.email, e.institution, r.dietary_restrictions, e.eats_oct15, e.eats_oct16
  FROM effective_attendance e JOIN registrations_corrected r USING(response_id)
  WHERE e.holds_seat = 1
  ORDER BY e.email LIKE ?, lower(e.last_name), lower(e.first_name)""", ("%@" + VENDOR,)).fetchall()

ORDER = ["None", "Vegetarian", "Vegan", "Pescatarian", "Halal", "No pork", "Gluten-free",
         "Dairy-free", "Gluten-free and dairy-free", "Shellfish allergy", "Nut allergy",
         "Other allergy / multiple", "Not yet known"]
bold = Font(bold=True)
fill = PatternFill("solid", fgColor="DDE5F0")

wb = Workbook()
ws = wb.active; ws.title = "Attendees"
ws.append(["First name", "Last name", "Email", "Institution", "Dietary restriction", "Details", "Eating Oct 15", "Eating Oct 16", "Thu lunch table", "Thu dinner table", "As written on form"])
badges = []
clean = {}  # seating-sheet key -> corrected name
clean_inst = {}
counts, c15, c16 = Counter(), Counter(), Counter()
for fn, ln, email, inst, diet, d15, d16 in rows:
    email = (email or "").strip().lower()
    key = email + ("|" + f"{fn} {ln}" if VENDOR in email else "")
    lt, dt = LUNCH.get(key, ""), DINNER.get(key, "")
    first, last = badge_name(fn, ln, email)
    clean[key] = f"{first} {last}"
    inst = badge_inst(inst)
    clean_inst[key] = inst
    if not (d15 or d16):
        ws.append([first, last, email, inst, "Not eating", "", "No", "No", "", "", (diet or "").strip()])
        badges.append([first, last, inst, "", ""])
        continue
    if (email or "").lower().endswith("@" + VENDOR):
        # ASL interpreters: the agency passes their restrictions to the event planner directly once assigned
        cat, note = "Not yet known", "Agency will send once interpreters are assigned"
    else:
        cat, note = normalize(diet)
    counts[cat] += 1; c15[cat] += d15; c16[cat] += d16
    ws.append([first, last, email, inst, cat, note,
               "Yes" if d15 else "No", "Yes" if d16 else "No", lt, dt, (diet or "").strip()])
    if VENDOR not in email:
        badges.append([first, last, inst, lt, dt])
assert set(counts) <= set(ORDER), set(counts) - set(ORDER)
for c in ws[1]: c.font = bold; c.fill = fill
ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
for i, w in enumerate([16, 20, 34, 36, 26, 44, 13, 13, 14, 15, 44], 1):
    ws.column_dimensions[get_column_letter(i)].width = w

s = wb.create_sheet("Summary", 0)
s.append(["Dietary restriction", "Attendees", "Oct 15", "Oct 16"])
for cat in ORDER:
    if counts[cat]: s.append([cat, counts[cat], c15[cat], c16[cat]])
s.append(["Total", sum(counts.values()), sum(c15.values()), sum(c16.values())])
for c in s[1] + s[s.max_row]: c.font = bold
for c in s[1]: c.fill = fill
s.append([])
s.append(["Counts are a ceiling. Most attendees have not yet confirmed which days they will eat, so they are counted for both days."])
s.column_dimensions["A"].width = 30
for col in "BCD": s.column_dimensions[col].width = 12
for title, src in (("Thu lunch seating", lunch_rows), ("Thu dinner seating", dinner_rows)):
    t = wb.create_sheet(title)
    t.append(["Table", "Seat", "Name", "Institution", "Dietary"])
    for r in src:
        k = (r[8] or "").strip().lower()
        k += ("|" + r[3]) if VENDOR in k else ""
        t.append([r[0], r[2], clean.get(k, r[3]), clean_inst.get(k, r[4]), r[7]])
    for c in t[1]: c.font = bold; c.fill = fill
    t.freeze_panes = "A2"; t.auto_filter.ref = t.dimensions
    for col, w in zip("ABCDE", [8, 7, 30, 40, 40]): t.column_dimensions[col].width = w

b = wb.create_sheet("Badges")
b.append(["First name", "Last name", "Institution", "Thu lunch table (back)", "Thu dinner table (back)"])
for row in sorted(badges, key=lambda x: (x[1].lower(), x[0].lower())):
    b.append(row)
for c in b[1]: c.font = bold; c.fill = fill
b.freeze_panes = "A2"
for col, w in zip("ABCDE", [18, 22, 44, 20, 20]): b.column_dimensions[col].width = w

# ---- By table: one row per table, with why it is grouped the way it is
from openpyxl.styles import Alignment
TOOL_DESC = {"LLMs": "large language models", "Text mining/NLP": "text mining and NLP",
    "GIS/mapping": "GIS and mapping", "Networks": "network analysis",
    "Databases": "historical databases", "ML/CV": "machine learning and computer vision",
    "OCR": "OCR and handwriting recognition", "Archives": "AI in the archive",
    "Environment": "environmental history", "Pedagogy": "teaching with AI", "Plenary": "the plenary sessions"}
MATCHABLE = {"LLMs", "Text mining/NLP", "GIS/mapping", "Networks", "Databases", "ML/CV", "OCR"}
# Same session -> theme map build_seating_xlsx.py uses to anchor lunch tables.
SESSION_TOOL = {4:"GIS/mapping",18:"GIS/mapping",25:"GIS/mapping",26:"OCR",19:"OCR",3:"OCR",
 15:"Networks",23:"Databases",10:"Databases",7:"Archives",13:"ML/CV",5:"Databases",12:"Databases",
 24:"Pedagogy",14:"Pedagogy",6:"Pedagogy",20:"Pedagogy",8:"LLMs",11:"LLMs",16:"LLMs",
 21:"Text mining/NLP",22:"Environment",2:"Plenary",9:"Plenary",17:"Plenary"}
sess = {e.lower(): ss for e, ss in con.execute("SELECT email, sessions FROM panelists")}
for a, c in con.execute("SELECT lower(alt_email), lower(canonical_email) FROM alternate_emails"):
    if c in sess: sess[a] = sess[c]
def join(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
def pinfo(r):
    """(display name, sessions text, that panelist's own lunch theme)."""
    e = (r[8] or "").lower()
    ss = [x.strip() for x in str(sess.get(e, "")).split(",") if x.strip()]
    lab = ("session " if len(ss) == 1 else "sessions ") + join(ss) if ss else ""
    theme = SESSION_TOOL.get(int(ss[0]), "Other") if ss else "Other"
    return clean.get(e, r[3]), lab, theme
def lunch_why(theme, prs):
    on = [f"{n} ({l})" for n, l, t in prs if t == theme]
    off = [f"{n} ({l})" for n, l, t in prs if t != theme]
    if theme in MATCHABLE:
        txt = f"For people who said on the form that they work with {TOOL_DESC[theme]}."
    elif theme == "Mixed":
        txt = "No theme."
    else:
        txt = (f"Themed on {TOOL_DESC.get(theme, theme)}. No form answer maps to this theme, so the other "
               "seats went to people whose theme tables were full or who listed no tools.")
    if on:
        txt += f" {join(on)} {'presents' if len(on) == 1 else 'present'} on it, so people can talk shop with a speaker."
    if off:
        txt += f" {join(off)} {'is' if len(off) == 1 else 'are'} here to spread speakers across the room, not because of the theme."
    if theme in MATCHABLE:
        txt += " Leftover seats went to people whose answers matched no open table."
    return txt
DINNER_DESC = {"Archives, libraries & museums": "archives, libraries and museums",
    "Digital methods & pedagogy": "digital history and teaching",
    "Economic & labor history": "economic and labor history",
    "Intellectual & book history": "intellectual, religious and book history",
    "Military & war": "military history", "Political & diplomatic": "political and diplomatic history",
    "Race, slavery & indigenous": "the history of race, slavery and Indigenous peoples",
    "Science, tech & medicine": "the history of science, technology, medicine and the environment",
    "Social & cultural": "social and cultural history"}
def dinner_why(group):
    if group not in DINNER_DESC:
        return "People whose field we could not tell from the form, mixed together."
    return (f"For people who work in {DINNER_DESC[group]}, based on the field they gave on the form, "
            "so they meet others in their area. No more than two panelists per table.")
bt = wb.create_sheet("By table")
bt.append(["How the tables work"])
bt.append(["Thursday lunch groups people by the digital methods they said they use (OCR, GIS, language models and so on), "
           "and puts one or two speakers on that method at each table. Thursday dinner groups people by historical field instead, "
           "so everyone gets two different sets of tablemates. Friday lunch is open seating."])
bt.append([])
bt.append(["Meal", "Table", "Theme", "Why this table is grouped this way", "Seated here"])
hdr_row = bt.max_row
for meal, src, why in (("Thursday lunch", lunch_rows, "lunch"), ("Thursday dinner", dinner_rows, "dinner")):
    tables = {}
    for r in src:
        tables.setdefault(r[0], []).append(r)
    for tid in sorted(tables):
        rs = tables[tid]; theme = rs[0][1]
        prs = [pinfo(r) for r in rs if r[6] == "PANELIST"]
        note = lunch_why(theme, prs) if why == "lunch" else dinner_why(theme)
        if any(VENDOR in (r[8] or "") for r in rs):
            note += " The two ASL interpreters sit at this table."
        names = []
        for r in rs:
            k = (r[8] or "").lower(); k += ("|" + r[3]) if VENDOR in k else ""
            nm = clean.get(k, r[3]) + (" (panelist)" if r[6] == "PANELIST" else "")
            names.append(nm)
        bt.append([meal, tid, theme, note, "\n".join(names)])
bt["A1"].font = Font(bold=True, size=12)
bt["A2"].alignment = Alignment(wrap_text=True, vertical="top"); bt.merge_cells("A2:E2"); bt.row_dimensions[2].height = 48
for c in bt[hdr_row]: c.font = bold; c.fill = fill
for row in bt.iter_rows(min_row=hdr_row + 1):
    for c in row: c.alignment = Alignment(wrap_text=True, vertical="top")
    row[0].parent.row_dimensions[row[0].row].height = 15 * max(1, str(row[4].value).count("\n") + 1)
for col, w in zip("ABCDE", [16, 7, 24, 60, 34]): bt.column_dimensions[col].width = w
bt.freeze_panes = bt.cell(row=hdr_row + 1, column=1)

# ---- Late additions with a seat but no meal reservation
la = wb.create_sheet("Late adds, no meals")
la.append(["Added after catering closed. They have a seat and a badge but no meal reservation and no table."])
la["A1"].font = bold
la.append([])
la.append(["First name", "Last name", "Email", "Institution", "Note"])
for c in la[3]: c.font = bold; c.fill = fill
for fn, ln, em, inst, note in con.execute("""SELECT r.first_name, r.last_name, lower(trim(r.email)), r.institution, e.notes
        FROM attendance_extent e JOIN registrations_corrected r USING(response_id)
        WHERE e.source = 'late-add-no-meals' ORDER BY lower(r.last_name)"""):
    first, last = badge_name(fn, ln, em)
    la.append([first, last, em, badge_inst(inst), note])
for col, w in zip("ABCDE", [16, 20, 30, 34, 60]): la.column_dimensions[col].width = w

# ---- Conversation starters, written per table from its theme, its speakers'
# sessions and what the people seated there said they work on.
if os.path.exists("scripts/table_starters.json"):
    cs = wb.create_sheet("Conversation starters")
    cs.append(["Conversation starters for each Thursday table. Print one card per table. No attendee is named or quoted."])
    cs["A1"].font = bold
    cs.append([])
    cs.append(["Meal", "Table", "Theme", "Question 1", "Question 2", "Question 3", "Question 4"])
    for c in cs[3]: c.font = bold; c.fill = fill
    theme_of = {(r[0], r[1]): r[2] for r in bt.iter_rows(min_row=hdr_row + 1, values_only=True)}
    for e in json.load(open("scripts/table_starters.json")):
        cs.append([e["meal"], e["table"], theme_of.get((e["meal"], e["table"]), "")] + e["questions"])
    for row in cs.iter_rows(min_row=4):
        for c in row: c.alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDEFG", [16, 7, 22, 40, 40, 40, 40]): cs.column_dimensions[col].width = w
    cs.freeze_panes = "D4"

# ---- Badge mockup image, if rendered
MOCK = os.environ.get("BADGE_MOCKUP")
if MOCK and os.path.exists(MOCK):
    from openpyxl.drawing.image import Image as XLImage
    m = wb.create_sheet("Badge mockup")
    m.append(["Badge mockup (4 x 3 in). Front: name and institution. Back: the same, plus Thursday table numbers in small type."])
    m["A1"].font = bold
    m.add_image(XLImage(MOCK), "A3")

s.append([])
for line in ["Badges: 4 x 3 in, printed on both sides. First and last name centred, institution below.",
             "Back only: Thursday lunch table in small type in the lower left corner, Thursday dinner table in the lower right.",
             "Leave a corner blank where the person has no table for that meal. Friday lunch is open seating.",
             "See the Badge mockup tab for the layout and the By table tab for who sits where and why.",
             "Lanyards: custom printed with \"AI and History Conference 2026\".",
             f"Badges to print: {len(badges)} (the two ASL interpreters are not included)."]:
    s.append([line])
wb.save(OUT)

print(OUT, len(rows), 'badges', len(badges))
for cat in ORDER:
    if counts[cat]: print(f"{cat}: {counts[cat]} (15th {c15[cat]}, 16th {c16[cat]})")
print("Total", sum(counts.values()), sum(c15.values()), sum(c16.values()))
