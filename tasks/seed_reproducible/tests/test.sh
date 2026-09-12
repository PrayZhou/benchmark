#!/usr/bin/env bash
# Verifier for seed_reproducible: the artifact must equal the injected seed.
set -euo pipefail

test -n "${BENCHMARK_SEED:-}"
test -f seed.txt
test "$(cat seed.txt)" = "$BENCHMARK_SEED"

echo "OK: seed=$BENCHMARK_SEED"
