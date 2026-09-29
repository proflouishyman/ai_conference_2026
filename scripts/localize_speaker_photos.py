#!/usr/bin/env python3
"""Download every hotlinked speaker photo into assets/speakers/ and rewrite
index.html to point at the local copy.

Hotlinked headshots break in two ways that are invisible until someone loads
the page: the institution blocks external embedding (403), or it reorganises
its site and the URL dies. Four were already broken when this was written.
Local copies fix both, and mean the page does not leak visitor traffic to a
dozen university servers.

Usage:
    python3 localize_speaker_photos.py           # dry run
    python3 localize_speaker_photos.py --apply
"""
import argparse
import html
import os
import re
import ssl
import unicodedata
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
INDEX = os.path.join(ROOT, "index.html")
OUTDIR = os.path.join(ROOT, "assets", "speakers")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
       "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}


def slug(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")
    return n


def fetch(url, referer=None):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "image/avif,image/webp,image/*,*/*;q=0.8")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    if referer:
        req.add_header("Referer", referer)
    ctx = ssl.create_default_context()
    # Some university hosts still negotiate legacy TLS.
    ctx.options |= 0x00040000  # OP_LEGACY_SERVER_CONNECT
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            data = r.read()
            if r.status != 200:
                return None, f"HTTP {r.status}"
            if not ctype.startswith("image/"):
                return None, f"not an image ({ctype or 'unknown'})"
            if len(data) < 500:
                return None, f"suspiciously small ({len(data)} bytes)"
            return (data, ctype), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"unreachable ({str(exc.reason)[:50]})"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:50]}"


def origin(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) + "/" if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(INDEX, encoding="utf-8") as fh:
        page = fh.read()

    cards = re.findall(r'<div class="speaker-card">(.*?)</div>\s*</div>',
                       page, re.S)
    jobs = []
    for c in cards:
        nm = re.search(r'<div class="speaker-name">(.*?)</div>', c)
        im = re.search(r'<img class="speaker-photo" src="(.*?)"', c)
        if nm and im:
            url = html.unescape(im.group(1))
            if url.startswith("assets/"):
                continue                      # already local
            jobs.append((html.unescape(nm.group(1)).strip(), url))

    print(f"{len(jobs)} hotlinked photo(s) to pull\n")
    if args.apply:
        os.makedirs(OUTDIR, exist_ok=True)

    ok = fail = 0
    failures = []
    for name, url in jobs:
        got, err = fetch(url, referer=origin(url))
        if not got:
            # retry once without a referer; some hosts dislike a foreign one
            got, err2 = fetch(url)
            if not got:
                print(f"  FAIL  {name:<26} {err}")
                failures.append((name, url, err))
                fail += 1
                continue
        data, ctype = got
        fname = slug(name) + EXT.get(ctype, ".jpg")
        rel = f"assets/speakers/{fname}"
        print(f"  OK    {name:<26} {len(data)//1024:>4} KB  -> {rel}")
        ok += 1
        if args.apply:
            with open(os.path.join(OUTDIR, fname), "wb") as fh:
                fh.write(data)
            page = page.replace(f'src="{html.escape(url, quote=True)}"',
                                f'src="{rel}"')
            page = page.replace(f'src="{url}"', f'src="{rel}"')

    if args.apply:
        with open(INDEX, "w", encoding="utf-8") as fh:
            fh.write(page)

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {ok} pulled, {fail} failed")
    if failures:
        print("\nCould not pull (these need a photo supplied by hand, or the "
              "img tag removed so the initials avatar shows):")
        for name, url, err in failures:
            print(f"  {name}: {err}\n    {url}")


if __name__ == "__main__":
    main()
