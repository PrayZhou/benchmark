"""FizzBuzz implementation. Some inputs produce the wrong label."""

from __future__ import annotations


def fizzbuzz(n: int) -> str:
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    return str(n)


def fizzbuzz_sequence(limit: int) -> list[str]:
    return [fizzbuzz(value) for value in range(1, limit + 1)]


if __name__ == "__main__":
    print(" ".join(fizzbuzz_sequence(15)))
