# Mobile & SEO Audit — AHA AI and History Conference 2026 site

Audited 2026-07-31. Screenshots taken at 375×812 (mobile) and 768×1024 (tablet) via headless Chrome for all five pages: `index.html`, `register.html`, `thanks.html`, `hotels.html`, `getting-here.html`. Confirmed via `document.documentElement.scrollWidth` vs `clientWidth` measurements, not just visual inspection.

**Bottom line:** `register.html`, `thanks.html`, `hotels.html`, and `getting-here.html` are already solidly mobile-responsive — confirmed zero horizontal overflow at 375px on all four, and their card/grid/form layouts degrade cleanly. All three real problems are in `index.html`. Fix those three and the site is mobile-clean.

---

## PART 1: MOBILE

### Problem 1 (CRITICAL) — Program table causes page-wide horizontal scroll

**Evidence:** `document.documentElement.scrollWidth` = 452px against a 375px viewport (77px overflow) — the whole page scrolls sideways, not just the table. Screenshot confirms the Track and Room columns are cut off at the right edge mid-row ("Ro..." truncated), and rows deep inside a `rowspan` block lose their Time value entirely from view since nothing re-displays it as you scroll.

**Root cause:** `.program-table` is a real `<table>` with a `<thead>` (Time/Session/Track/Room) and several `rowspan`-grouped Time cells (one Time cell spans 4-5 rows per time block). There's an existing `@media (max-width: 640px)` block that only shrinks font-size and padding — not enough to fit 4 columns plus rich paragraph-length session descriptions into 375px.

**Recommended approach:** convert the table to a stacked "card" layout at narrow widths (not a horizontal-scroll wrapper). Reasoning: this table's first column ("Session") contains full session titles, presenter names, and 2-4 sentence descriptions — a horizontal-scroll table would force users to scroll sideways mid-paragraph to read a single session, which is worse than just stacking. Every serious conference site handles a mobile schedule as a card list, not a scrollable table.

**The blocker:** `rowspan` is meaningless once you switch to `display:block` (the standard CSS technique for turning a table into stacked cards) — the Time value would only appear once per group and be orphaned from the rows below it. **Fix requires one minimal HTML change:** flatten every `rowspan` group by removing the `rowspan="N"` attribute and repeating the Time `<td>` on every row in that group, so every `<tr>` uniformly has 4 `<td>`s (Time, Session, Track, Room). This has zero effect on desktop rendering (rowspan just becomes 6 separate one-row Time cells stacked in the same visual column — harmless) and makes the mobile CSS below reliable via `nth-of-type`.

**Exact rows needing the Time `<td>` added** (currently these rows only have 3 `<td>`s — Session, Track, Room — because they're inside a `rowspan` group):

| Time block | Remove `rowspan="N"` from | Add `<td class="time-cell">TIME</td>` as new first cell in these rows |
|---|---|---|
| Day 1, 10:45 – 12:15 | session #3's row (`rowspan="5"`) | #3b, #4, #5, #6 |
| Day 1, 1:15 – 2:45 | session #7's row (`rowspan="4"`) | #8, #9, #10 |
| Day 1, 3:00 – 4:30 | session #11's row (`rowspan="4"`) | #12, #13, #14 |
| Day 2, 10:45 – 12:15 | session #16's row (`rowspan="5"`) | #17, #18, #19, #19b |
| Day 2, 1:15 – 2:45 | session #20's row (`rowspan="4"`) | #21, #22, #23 |
| Day 2, 3:00 – 4:30 | session #24's row (`rowspan="4"`) | #25, #26, #27 |

(Use the exact same time-range text already on the group's first row — e.g. `<td class="time-cell">10:45 – 12:15</td>` — for every row in that group.)

**CSS to add** — insert inside the *existing* `@media (max-width: 640px)` block in `index.html` (the one that currently only has `.program-table { font-size: 0.8rem; }` etc. — don't create a second, duplicate media query):

```css
.program-table thead { display: none; }

.program-table,
.program-table tbody,
.program-table tr,
.program-table td {
  display: block;
  width: 100%;
}

.program-table tr {
  margin-bottom: 0.85rem;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 6px rgba(0,0,0,0.08);
}

.program-table td {
  border-bottom: none;
}

.program-table .time-cell {
  background: rgba(0,45,114,0.07);
  min-width: 0;
  white-space: normal;
}

/* Track cell (3rd td) and Room cell (4th td) become inline mini-labels */
.program-table td:nth-of-type(3),
.program-table td:nth-of-type(4) {
  display: inline-block;
  width: auto;
  padding: 0.3rem 0.9rem;
  vertical-align: middle;
}

.program-table td:nth-of-type(3)::before {
  content: "Track ";
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #888;
  margin-right: 0.3rem;
}

.program-table td:nth-of-type(4)::before {
  content: "Room: ";
  font-size: 0.65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #888;
}
```

This relies on every row uniformly having Time=1st, Session=2nd, Track=3rd, Room=4th `<td>` — which is exactly what flattening the rowspan groups above guarantees.

---

### Problem 2 — Hero co-branding logos take over the whole first screen on mobile

**Evidence:** screenshot shows the AHA/JHU logo row (JHU logo fixed at `height:180px`, plus an adjacent 1px-wide, 180px-tall divider) wrapping on mobile and pushing the actual `<h1>` conference title down past the fold — a user has to scroll to see the page even has a title.

**Fix — add two classes so the media query can target them cleanly** (minimal HTML change), then add the CSS:

In the hero's co-branding row (around line 781-790 of `index.html`), add `class="hero-logo-jhu"` to the JHU `<img>` tag and `class="hero-logo-divider"` to the divider `<div>`:

```html
<img src="https://logo.wine/a/logo/Johns_Hopkins_University/Johns_Hopkins_University-Logo.wine.svg"
     alt="Johns Hopkins University"
     class="hero-logo-jhu"
     style="height:180px;width:auto;filter:brightness(0) invert(1);opacity:0.95;"
     onerror="..." />
```
```html
<div class="hero-logo-divider" style="width:1px;height:180px;background:rgba(255,255,255,0.35);"></div>
```

**CSS to add** (new mobile media query, or fold into the existing 640px block):

```css
@media (max-width: 640px) {
  .hero-logo-jhu { height: 64px !important; }
  .hero-logo-divider { height: 64px !important; }
  #hero > .hero-content > div:first-child { gap: 1rem !important; margin-bottom: 1.5rem !important; }
}
```

---

### Problem 3 — Agora photo gallery has zero mobile treatment

**Evidence:** `#agora`'s photo grid uses inline `style="display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem;"` with no media query anywhere — on a 375px screen this produces three ~105px-wide photo columns, each cropped to a tall sliver via `object-fit:cover`. Not an overflow bug, just a bad crop/legibility problem.

**Fix — add a class, move the grid-template-columns out of the inline style** (minimal HTML change):

Change the photo-gallery `<div>` (around line 1551) from:
```html
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem;">
```
to:
```html
<div class="agora-photo-grid" style="display:grid;gap:1.25rem;">
```

**CSS to add:**
```css
.agora-photo-grid { grid-template-columns: repeat(3, 1fr); }

@media (max-width: 640px) {
  .agora-photo-grid { grid-template-columns: 1fr; }
}
```

---

### Everything else — no changes needed

- **Nav/hamburger**: already has a clean `max-width: 768px` breakpoint, tap targets are adequate (padding gives >44px effective height on the collapsed menu links).
- **`.venue-grid`, `.speakers-grid`, `.conversation-list`**: already `grid-template-columns: repeat(auto-fill, minmax(...))` or an explicit `@media (max-width: 700px)` override — confirmed via screenshot these stack to a single column cleanly on mobile.
- **`register.html`**: confirmed zero horizontal overflow at 375px. Form fields are full-width, labels readable, and `.experience-grid` (the 4-card radio picker) already has `@media { grid-template-columns: 1fr; }` at its own breakpoint (line 362) — stacks correctly. No changes recommended.
- **`thanks.html`**: confirmed zero overflow, clean stacking, numbered timeline reads fine at 375px. No changes recommended.
- **`hotels.html`**: confirmed zero overflow. `.hotel-card`'s `flex-wrap: wrap` layout degrades exactly as intended — name/description stack above price-tag/book-link. No changes recommended.
- **`getting-here.html`**: confirmed zero overflow, same card pattern as hotels.html, degrades cleanly. No changes recommended.
- **Minor, optional polish**: `hotels.html` has no `<h2>` anywhere — each `.hotel-name` is a `<div>`, not a heading. Not a functional bug, but converting `<div class="hotel-name">` to `<h2 class="hotel-name">` (identical visual styling, just a semantic tag swap) would improve both accessibility (screen-reader section navigation) and SEO structure. Optional, low-priority.

---

## PART 2: SEO

### Current state (confirmed via grep across all 5 files)
- All 5 pages have distinct, reasonable `<title>` tags already — no changes needed there.
- **Zero** `<meta name="description">` tags anywhere.
- **Zero** Open Graph or Twitter Card tags anywhere.
- **Zero** `<link rel="canonical">` tags anywhere.
- No `robots.txt` or `sitemap.xml` in the repo.
- Heading structure is clean on 4 of 5 pages (one `<h1>` per page, no skipped levels, logical `<h2>`/`<h3>` nesting) — see the `hotels.html` note above for the one minor gap.

### Ready-to-paste `<head>` additions, per page

Insert each block right after the existing `<title>` line. Site base URL: `https://proflouishyman.github.io/ai_conference_2026/`.

**`index.html`:**
```html
<meta name="description" content="A practitioner-focused conference for historians using AI and computational methods, October 15–16, 2026 at Johns Hopkins University — an official conference of the American Historical Association." />
<link rel="canonical" href="https://proflouishyman.github.io/ai_conference_2026/" />
<meta property="og:title" content="AHA AI and History Conference 2026" />
<meta property="og:description" content="A practitioner-focused conference for historians using AI and computational methods, October 15–16, 2026 at Johns Hopkins University." />
<meta property="og:type" content="website" />
<meta property="og:url" content="https://proflouishyman.github.io/ai_conference_2026/" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="AHA AI and History Conference 2026" />
<meta name="twitter:description" content="A practitioner-focused conference for historians using AI and computational methods, October 15–16, 2026 at Johns Hopkins University." />
```

Also add this JSON-LD `Event` block (index.html only — not the other four pages) just before `</head>`:
```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Event",
  "name": "AHA AI and History Conference 2026",
  "description": "A practitioner-focused conference for historians using AI and computational methods, in partnership with the American Historical Association.",
  "startDate": "2026-10-15",
  "endDate": "2026-10-16",
  "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
  "eventStatus": "https://schema.org/EventScheduled",
  "location": {
    "@type": "Place",
    "name": "Johns Hopkins University, Homewood Campus (Agora)",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Baltimore",
      "addressRegion": "MD",
      "addressCountry": "US"
    }
  },
  "organizer": {
    "@type": "Person",
    "name": "Louis Hyman",
    "email": "lhyman6@jh.edu"
  },
  "url": "https://proflouishyman.github.io/ai_conference_2026/"
}
</script>
```

**`register.html`:**
```html
<meta name="description" content="Register or apply to attend the AHA AI and History Conference 2026, October 15–16 at Johns Hopkins University." />
<link rel="canonical" href="https://proflouishyman.github.io/ai_conference_2026/register.html" />
<meta property="og:title" content="Register — AHA AI and History Conference 2026" />
<meta property="og:description" content="Register or apply to attend the AHA AI and History Conference 2026, October 15–16 at Johns Hopkins University." />
<meta property="og:type" content="website" />
<meta property="og:url" content="https://proflouishyman.github.io/ai_conference_2026/register.html" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="Register — AHA AI and History Conference 2026" />
<meta name="twitter:description" content="Register or apply to attend the AHA AI and History Conference 2026, October 15–16 at Johns Hopkins University." />
```

**`thanks.html`** — this is a post-submission confirmation page, not something that should show up in search results. Add `noindex` instead of full OG treatment:
```html
<meta name="robots" content="noindex, nofollow" />
<meta name="description" content="Confirmation page for AHA AI and History Conference 2026 applications." />
<link rel="canonical" href="https://proflouishyman.github.io/ai_conference_2026/register.html" />
```
(Canonical intentionally points back to `register.html` rather than to itself, since this page has no independent search value and shouldn't be indexed as a separate destination.)

**`hotels.html`:**
```html
<meta name="description" content="Suggested hotels near Johns Hopkins University's Homewood Campus for attendees of the AHA AI and History Conference 2026." />
<link rel="canonical" href="https://proflouishyman.github.io/ai_conference_2026/hotels.html" />
<meta property="og:title" content="Suggested Hotels — AHA AI and History Conference 2026" />
<meta property="og:description" content="Suggested hotels near Johns Hopkins University's Homewood Campus, across a range of price points and distances." />
<meta property="og:type" content="website" />
<meta property="og:url" content="https://proflouishyman.github.io/ai_conference_2026/hotels.html" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="Suggested Hotels — AHA AI and History Conference 2026" />
<meta name="twitter:description" content="Suggested hotels near Johns Hopkins University's Homewood Campus, across a range of price points and distances." />
```

**`getting-here.html`:**
```html
<meta name="description" content="Parking, train, and campus-access information for attendees of the AHA AI and History Conference 2026 at Johns Hopkins University." />
<link rel="canonical" href="https://proflouishyman.github.io/ai_conference_2026/getting-here.html" />
<meta property="og:title" content="Getting to Campus — AHA AI and History Conference 2026" />
<meta property="og:description" content="Parking at JHU, trains to Baltimore, and getting from Baltimore Penn Station to the Homewood Campus." />
<meta property="og:type" content="website" />
<meta property="og:url" content="https://proflouishyman.github.io/ai_conference_2026/getting-here.html" />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="Getting to Campus — AHA AI and History Conference 2026" />
<meta name="twitter:description" content="Parking at JHU, trains to Baltimore, and getting from Baltimore Penn Station to the Homewood Campus." />
```

No `og:image` added anywhere — there's no image asset in the repo suited for a social-share card (the JHU/AHA logos are third-party-hosted SVGs, not something to claim as this site's preview image). Worth creating a proper 1200×630 og:image later, but don't invent one now.

### `robots.txt` (new file, repo root)

```
User-agent: *
Allow: /

Sitemap: https://proflouishyman.github.io/ai_conference_2026/sitemap.xml
```

### `sitemap.xml` (new file, repo root)

`thanks.html` is deliberately excluded — it's a post-submission confirmation page with no independent search value (matches the `noindex` recommendation above).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://proflouishyman.github.io/ai_conference_2026/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://proflouishyman.github.io/ai_conference_2026/register.html</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://proflouishyman.github.io/ai_conference_2026/hotels.html</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://proflouishyman.github.io/ai_conference_2026/getting-here.html</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>
```
