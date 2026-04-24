#!/usr/bin/env bash
# Regenerate the PROVIDER_ICONS block in ui/index.html from the canonical SVGs
# at Fathom/web/design/public/v1/icons/.
#
# When the design repo's icon SVGs change, run this to refresh the inlined
# copies here. The block lives between the sentinel comments in ui/index.html.

set -euo pipefail
cd "$(dirname "$0")/.."

DESIGN_DIR=../web/design/public/v1/icons
TARGET=ui/index.html

if [ ! -d "$DESIGN_DIR" ]; then
  echo "design repo not found at $DESIGN_DIR" >&2
  exit 1
fi

python3 - <<PY
import os, re
DESIGN = "$DESIGN_DIR"
TARGET = "$TARGET"
slugs = ['claude-code', 'apple', 'windows', 'linux']

lines = ["    // ── PROVIDER_ICONS (generated from $DESIGN_DIR) ──"]
lines.append("    const PROVIDER_ICONS = {")
for s in slugs:
    with open(os.path.join(DESIGN, s + '.svg')) as f:
        content = f.read().rstrip()
    escaped = content.replace('\\\\', '\\\\\\\\').replace('\`', '\\\\\`').replace('\${', '\\\\\${')
    lines.append(f"      '{s}': \`{escaped}\`,")
lines.append("    };")
lines.append("    // ── /PROVIDER_ICONS ──")
block = "\\n".join(lines)

with open(TARGET) as f:
    html = f.read()

pat = re.compile(r"    // ── PROVIDER_ICONS \(.*?\) ──.*?    // ── /PROVIDER_ICONS ──", re.S)
if not pat.search(html):
    raise SystemExit("sentinel comments not found in " + TARGET)
html = pat.sub(block.replace("\\\\", "\\\\\\\\"), html)

with open(TARGET, 'w') as f:
    f.write(html)

print("updated", TARGET)
PY
