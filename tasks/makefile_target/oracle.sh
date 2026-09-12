#!/usr/bin/env bash
# Reference solution: point the recipe at the declared input variable.
set -euo pipefail

python3 - <<'PY'
from pathlib import Path

makefile = Path("Makefile")
text = makefile.read_text(encoding="utf-8")
broken = "sort data.txt > $(OUTPUT)"
if broken not in text:
    raise SystemExit("makefile_target oracle: expected broken recipe not found")
makefile.write_text(text.replace(broken, "sort $(INPUT) > $(OUTPUT)", 1), encoding="utf-8")
PY
