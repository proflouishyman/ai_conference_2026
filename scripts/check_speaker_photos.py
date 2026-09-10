#!/usr/bin/env python3
"""Verify every speaker photo on index.html actually loads.

A broken headshot on a live conference site looks worse than no photo, and
the page's own fallback only fires in the browser. This checks each URL
server-side so problems are caught before a commit.

Usage:
    python3 check_speaker_photos.py            # check what is in index.html
    python3 check_speaker_photos.py --url X    # check one URL before adding it
"""
import argparse
import html
import os
import re
import ssl
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, os.pardir, "index.html")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif",
               "image/jpg", "image/avif")


def check(url, timeout=15):
    """Return (ok, detail). Uses GET, since some hosts refuse HEAD."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "image/*,*/*")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            blob = r.read(2048)
            if r.status != 200:
                return False, f"HTTP {r.status}"
            if not ctype.startswith("image/"):
                return False, f"not an image (Content-Type: {ctype or 'none'})"
            if len(blob) < 100:
                return False, "response too small to be an image"
            return True, f"{ctype}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"unreachable ({str(exc.reason)[:60]})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="check a single URL instead of the page")
    args = ap.parse_args()

    if args.url:
        ok, detail = check(args.url)
        print(f"{'OK  ' if ok else 'FAIL'}  {detail}  {args.url}")
        raise SystemExit(0 if ok else 1)

    with open(INDEX, encoding="utf-8") as fh:
        page = fh.read()

    cards = re.findall(r'<div class="speaker-card">(.*?)</div>\s*</div>',
                       page, re.S)
    named = 0
    photos = []
    for c in cards:
        nm = re.search(r'<div class="speaker-name">(.*?)</div>', c)
        im = re.search(r'<img class="speaker-photo" src="(.*?)"', c)
        if nm:
            named += 1
            if im:
                photos.append((html.unescape(nm.group(1)).strip(),
                               html.unescape(im.group(1))))

    print(f"{named} speaker cards, {len(photos)} with a photo, "
          f"{named - len(photos)} using the initials fallback\n")

    bad = []
    for name, url in photos:
        ok, detail = check(url)
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<26} {detail}")
        if not ok:
            bad.append((name, url, detail))

    if bad:
        print(f"\n{len(bad)} broken photo(s) — remove the img tag and let the "
              f"initials avatar take over:")
        for name, url, detail in bad:
            print(f"  {name}: {detail}\n    {url}")
        raise SystemExit(1)
    print("\nEvery photo loads.")


if __name__ == "__main__":
    main()
