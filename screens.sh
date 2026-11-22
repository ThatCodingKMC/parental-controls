#!/usr/bin/env bash
# Pull Adam's screenshots from his Pi and open them as a gallery.
#
# Usage:
#   ./screens.sh          — sync and open the gallery (newest first)
#   ./screens.sh 2026-11-21 — only show that day
#
# Screenshots are taken every 1–4 min (random) while he's logged in, tagged
# FREE or WORK by the mode at the time. The Pi keeps 7 days. Copies here are
# kept in ~/adam-screens until you delete them.

set -euo pipefail

TARGET="adams-pc"
DEST="$HOME/adam-screens"
DAY="${1:-}"

mkdir -p "$DEST"
echo "-> Syncing screenshots from $TARGET..."
rsync -a --rsync-path="sudo rsync" "$TARGET:/var/lib/adam-control/screens/" "$DEST/"

python3 - "$DEST" "$DAY" <<'PY'
import html, sys
from pathlib import Path
root, only = Path(sys.argv[1]), sys.argv[2]
days = sorted((d for d in root.iterdir() if d.is_dir() and (not only or d.name == only)), reverse=True)
parts, total = [], 0
for d in days:
    shots = sorted(d.glob("*.jpg"), reverse=True)
    if not shots:
        continue
    total += len(shots)
    parts.append(f"<h2>{d.name} <small>{len(shots)} shots</small></h2><div class=g>")
    for s in shots:
        t, _, mode = s.stem.partition("_")
        hhmm = f"{int(t[:2]) % 12 or 12}:{t[2:4]}:{t[4:6]} {'PM' if int(t[:2]) >= 12 else 'AM'}"
        src = html.escape(f"{d.name}/{s.name}")
        parts.append(f'<a href="{src}" target=_blank class="{html.escape(mode)}"><img loading=lazy src="{src}">'
                     f'<span>{hhmm} <b>{html.escape(mode.upper())}</b></span></a>')
    parts.append("</div>")
page = f"""<!doctype html><meta charset=utf-8><title>Adam's screen</title>
<style>
body{{font:14px system-ui,sans-serif;margin:16px;background:#111;color:#eee}}
h2 small{{color:#999;font-weight:normal}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}}
a{{color:inherit;text-decoration:none;background:#222;border-radius:6px;overflow:hidden;border:3px solid #444}}
a.work{{border-color:#c33}} a.free{{border-color:#3a3}}
img{{width:100%;display:block;aspect-ratio:16/9;object-fit:cover}}
span{{display:block;padding:4px 8px}}
</style>
<h1>Adam's screen</h1>
<p>Red = taken during WORK (school-only) time, green = FREE time. Click a shot to see it full size.</p>
{''.join(parts) or '<p>No screenshots yet.</p>'}"""
(root / "index.html").write_text(page)
print(f"-> {total} screenshots")
PY

xdg-open "$DEST/index.html" >/dev/null 2>&1 &
echo "-> Opened $DEST/index.html"
