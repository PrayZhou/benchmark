#!/usr/bin/env bash
# Verifier for makefile_target: force the recipe to run, then inspect the
# regenerated artifact. End state only (exit code + report.txt), hardened so a
# pre-fabricated artifact or an "up to date" target cannot pass.
#
# The verifier runs on a scratch copy of the workspace. Under Docker the
# workspace is mounted read-only (:ro), so `rm`/`make` in place would fail with
# EROFS even for a correct solution; all mutation happens in the copy instead.
set -euo pipefail

test -f Makefile

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cp -a . "$scratch/"
# ``cp -a`` can copy the workspace directory's own mode onto the scratch dir,
# so a read-only-looking workspace would leave the copy unwritable too. Force
# the copy (the only place this script mutates) to be writable by us.
chmod -R u+rwX "$scratch"
cd "$scratch"

# Remove any pre-built output and force the recipe to run, so a passing run
# proves the Makefile itself produced report.txt (and not a pre-created file or
# an "up to date" no-op).
rm -f report.txt
make -B report
test -f report.txt

expected="$(printf 'apple\napple\nbanana\ncherry\ndate')"
test "$(cat report.txt)" = "$expected"

# The recipe must consume the declared input rather than hardcode the result or
# copy a prepared file. Recipe lines are tab-indented per Makefile syntax;
# accept either the $(INPUT) variable or the literal input filename.
python3 - <<'PY'
from pathlib import Path

recipes = [line for line in Path("Makefile").read_text(encoding="utf-8").splitlines() if line.startswith("\t")]
assert recipes, "Makefile defines no recipe lines"
assert any("$(INPUT)" in line or "input.txt" in line for line in recipes), \
    "the recipe must read the declared input, not hardcode report.txt"
PY

echo "OK: report.txt regenerated from the declared input"
