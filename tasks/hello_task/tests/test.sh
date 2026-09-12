#!/usr/bin/env bash
set -euo pipefail
test -f marker.txt
test "$(cat marker.txt)" = READY
