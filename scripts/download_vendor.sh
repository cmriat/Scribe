#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="${SCRIPT_DIR}/.."
export VENDOR_ROOT="${PROJECT_ROOT}/scribe/vendor"

echo "[vendor] target: ${VENDOR_ROOT}"
mkdir -p "${VENDOR_ROOT}/alpinejs" \
         "${VENDOR_ROOT}/dygraphs" \
         "${VENDOR_ROOT}/tailwind" \
         "${VENDOR_ROOT}/three/examples/jsm/controls" \
         "${VENDOR_ROOT}/three/examples/jsm/loaders"

curl -fL --retry 3 -o "${VENDOR_ROOT}/alpinejs/cdn.min.js" \
  "https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.13.5/cdn.min.js"

curl -fL --retry 3 -o "${VENDOR_ROOT}/dygraphs/dygraph.min.js" \
  "https://cdn.jsdelivr.net/npm/dygraphs@2.2.1/dist/dygraph.min.js"

curl -fL --retry 3 -o "${VENDOR_ROOT}/tailwind/tailwindcss.js" \
  "https://cdn.tailwindcss.com"

curl -fL --retry 3 -o "${VENDOR_ROOT}/three/three.module.js" \
  "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"

curl -fL --retry 3 -o "${VENDOR_ROOT}/three/examples/jsm/controls/OrbitControls.js" \
  "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js"

curl -fL --retry 3 -o "${VENDOR_ROOT}/three/examples/jsm/loaders/STLLoader.js" \
  "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/STLLoader.js"


perl -0pi -e "s/from 'three';/from '..\/..\/..\/three.module.js';/"   "${VENDOR_ROOT}/three/examples/jsm/controls/OrbitControls.js"   "${VENDOR_ROOT}/three/examples/jsm/loaders/STLLoader.js"

echo "[vendor] rewrote jsm imports to local three.module.js"


python3 - <<'PYTH'
from pathlib import Path
import re
import os
p = Path(os.environ["VENDOR_ROOT"]) / "tailwind" / "tailwindcss.js"
text = p.read_text(encoding="utf-8", errors="ignore")
text = re.sub(
    r'console\.warn\("cdn\.tailwindcss\.com should not be used in production[^"]*"\);',
    'void 0;',
    text,
    count=1,
)
p.write_text(text, encoding="utf-8")
PYTH

echo "[vendor] silenced tailwind runtime warning"

echo "[vendor] downloaded successfully"
find "${VENDOR_ROOT}" -type f | sort

