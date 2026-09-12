#!/usr/bin/env bash
# Verifier for makefile_target: run the target, then inspect the artifact.
set -euo pipefail

make report
test -f report.txt

expected="$(printf 'apple\napple\nbanana\ncherry\ndate')"
test "$(cat report.txt)" = "$expected"

echo "OK: report.txt has $(wc -l < report.txt | tr -d ' ') lines"
