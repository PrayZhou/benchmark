#!/usr/bin/env bash
# Reference solution: write the seed the harness injected.
set -euo pipefail

printf '%s\n' "$BENCHMARK_SEED" > seed.txt
