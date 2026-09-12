# Fix the FizzBuzz bug

`fizzbuzz.py` in the working directory implements the FizzBuzz sequence but
produces the wrong label for some inputs.

Fix the bug. The public behaviour must be:

- `fizzbuzz(n)` returns `"FizzBuzz"` when `n` is divisible by both 3 and 5,
  `"Fizz"` when it is divisible by 3 only, `"Buzz"` when it is divisible by 5
  only, and the decimal string for `n` otherwise.
- `fizzbuzz_sequence(limit)` returns the list of those labels for `1..limit`
  inclusive.

Keep the function names and signatures unchanged. Do not add dependencies; the
module must import and run under plain `python3`.
