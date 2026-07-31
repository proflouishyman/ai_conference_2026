# AI and History Conference 2026

Conference website — October 15–16, 2026, Johns Hopkins University.

## Files

- `index.html` — Main conference site (hero, about, full two-day program, speakers, structured conversations, venue)
- `register.html` — Registration/application form (Formspree-powered, 5 sections)
- `thanks.html` — Post-submission thank-you page
- `.nojekyll` — Disables Jekyll processing on GitHub Pages

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
