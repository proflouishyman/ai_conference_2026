# AI and History Conference 2026

Conference website — October 15–16, 2026, Johns Hopkins University.

A practitioner-focused conference for historians using AI and computational methods, run as an official conference of the American Historical Association (AHA).

**Live site:** https://proflouishyman.github.io/ai_conference_2026

## Files

- `index.html` — Main conference site (hero, about, full two-day program, speakers, structured conversations, venue); includes mobile-responsive card layout and SEO/Open Graph/JSON-LD tags
- `register.html` — Registration page with an embedded Google Form (online attendance is free); responses are read by `scripts/sync_registrations.py` through the Google Forms API
- `thanks.html` — Post-submission thank-you page
- `who-is-coming.html` — Aggregate picture of registrants (counts, countries, institutions), built from `data/whos_coming.json`
- `share.html` — Ready-made text and links for sharing the conference
- `one-cool-thing.html` — Upload page for short "one cool thing" video or text submissions (backend in `apps_script/one_cool_thing/`)
- `hotels.html` — Nearby lodging options
- `getting-here.html` — Directions, parking, and shuttle info
- `scripts/` — Registration sync, stats, Discord server build and bots, badge/seating/catering builders
- `robots.txt`, `sitemap.xml` — SEO crawl/indexing config
- `.nojekyll` — Disables Jekyll processing on GitHub Pages
