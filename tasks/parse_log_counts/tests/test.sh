#!/usr/bin/env bash
# Verifier for parse_log_counts: checks the produced JSON against the log.
set -euo pipefail

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("out.json").read_text(encoding="utf-8"))
assert isinstance(payload, dict), f"out.json must contain a JSON object, got {type(payload).__name__}"

expected = {"INFO": 5, "WARN": 2, "ERROR": 3, "DEBUG": 2}
assert payload == expected, f"level counts mismatch: {payload!r}"

print(json.dumps(payload, sort_keys=True))
PY
