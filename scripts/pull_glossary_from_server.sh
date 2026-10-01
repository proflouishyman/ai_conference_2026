#!/bin/zsh
# Pull the live glossary.json (written by the bot on ces-server) back into the
# laptop repo so it can be reviewed and committed. Read-only on the server.
# Usage: scripts/pull_glossary_from_server.sh [host]   (default: ces-server)
set -euo pipefail
HOST="${1:-ces-server}"
REMOTE="coding/ai_conference_2026/scripts/glossary.json"
LOCAL="$(cd "$(dirname "$0")" && pwd)/glossary.json"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

scp -q "$HOST:$REMOTE" "$TMP"
python3 - "$TMP" <<'PY'
import json, re, sys
g = json.load(open(sys.argv[1]))
bad = [k for k, v in g.items()
       if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+|<@", json.dumps(v))]
assert g and not bad, f"refusing: empty file or address-like text in {bad}"
PY
if diff -q "$TMP" "$LOCAL" >/dev/null; then echo "glossary.json already up to date"; exit 0; fi
git -C "$(dirname "$LOCAL")" --no-pager diff --no-index --stat "$LOCAL" "$TMP" || true
cp "$TMP" "$LOCAL"
echo "Updated $LOCAL. Review with: git diff scripts/glossary.json"
echo "Then commit (the privacy hook still applies)."
