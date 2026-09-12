#!/usr/bin/env bash
# Reference solution: count the second whitespace-separated field of each line.
set -euo pipefail

python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path

counts = Counter()
for line in Path("input.log").read_text(encoding="utf-8").splitlines():
    fields = line.split()
    if len(fields) >= 2:
        counts[fields[1]] += 1

Path("out.json").write_text(json.dumps(dict(counts), indent=2) + "\n", encoding="utf-8")
PY
