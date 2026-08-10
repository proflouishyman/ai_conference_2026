# AI and History Conference 2026

Conference website — October 15–16, 2026, Johns Hopkins University.

## Files

### Site (public, deployed)

- `index.html` — Main conference site (hero, about, full two-day program, speakers, structured conversations, venue); includes mobile-responsive card layout and SEO/Open Graph/JSON-LD tags
- `register.html` — Registration/application form (Formspree-powered; free for grad students and financial-hardship cases, $100 for faculty/professional)
- `thanks.html` — Post-submission thank-you page
- `hotels.html` — Nearby lodging options
- `getting-here.html` — Directions, parking, and shuttle info
- `robots.txt`, `sitemap.xml` — SEO crawl/indexing config
- `.nojekyll` — Disables Jekyll processing on GitHub Pages

### Organizer working documents (not part of the public site)

- `OUTREACH.md` — Speaker outreach tracker: the full session grid, confirmed/invited speakers with draft invitation emails, and open sessions still needing a presenter. Source of truth for who's been contacted and the status of every session.
- `MAILING_LIST.md` / `MAILING_LIST.csv` — Broader candidate pool researched for speaker outreach, feeding into `OUTREACH.md` as people are promoted to active outreach.
- `correspondence/` — Local archive of real email threads (pulled read-only via the openclaw Gmail/Outlook bridge) relevant to speaker outreach, for Louis's and Claude's reference only — see `correspondence/README.md`.
- `MOBILE_AND_SEO_AUDIT.md` — Audit findings and fixes for mobile responsiveness and SEO.
- `SCHEDULE_FILLING_PLAN.md` — Planning notes for filling out the program grid.
- `registration_form_spec.json` — Field spec for the registration form, in the format the `google-forms` skill's `create_form.py` consumes (see `~/.claude/skills/google-forms/SKILL.md`). Used to migrate registration off Formspree onto a Google Form embedded in `register.html`.

## Setup: Registration Form

This site uses [Formspree](https://formspree.io) for form submissions (no server required).

1. Create a free account at [formspree.io](https://formspree.io)
2. Create a new form — copy the form ID (looks like `xabcdefg`)
3. In `register.html`, replace `REPLACE_WITH_YOUR_FORM_ID` with your form ID:
   ```
   action="https://formspree.io/f/YOUR_FORM_ID"
   ```
4. The form is already configured to redirect to `thanks.html` after submission

## Deploy to GitHub Pages

- Repo: https://github.com/proflouishyman/ai_conference_2026
- Settings → Pages → Source: Deploy from branch → `main` → `/ (root)`
- Site will be live at: https://proflouishyman.github.io/ai_conference_2026

## Design

- Colors: Navy `#002D72` | Gold `#FFD100` | White `#FFFFFF` | Light gray `#F7F8FA`
- Fonts: Source Serif 4 (headings) + Inter (body/UI) via Google Fonts
- CSS: Tailwind CDN + custom `<style>` block — no build step required
- JS: Vanilla only — tab switching, hamburger menu, conditional form fields

## Contact

Louis Hyman — lhyman6@jh.edu
