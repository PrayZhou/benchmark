#!/usr/bin/env bash
# Verifier for fix_fizzbuzz: checks the corrected module's behaviour only.
set -euo pipefail

python3 - <<'PY'
import json

from fizzbuzz import fizzbuzz, fizzbuzz_sequence

cases = {
    1: "1",
    2: "2",
    3: "Fizz",
    5: "Buzz",
    6: "Fizz",
    10: "Buzz",
    15: "FizzBuzz",
    30: "FizzBuzz",
    98: "98",
}
for value, expected in cases.items():
    actual = fizzbuzz(value)
    assert actual == expected, f"fizzbuzz({value}) == {actual!r}, expected {expected!r}"

expected_sequence = [
    "1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz",
    "Buzz", "11", "Fizz", "13", "14", "FizzBuzz",
]
actual_sequence = fizzbuzz_sequence(15)
assert actual_sequence == expected_sequence, actual_sequence

print(json.dumps({"cases": len(cases), "sequence_length": len(actual_sequence)}))
PY
