# Reproduce the injected seed

The harness injects a deterministic integer seed into every environment as the
`BENCHMARK_SEED` environment variable. On this trial the seed is nonzero.

Write a file named `seed.txt` in the working directory whose contents are the
seed value itself. A single trailing newline is allowed. Do not hard-code the
number: read it from the environment so the task stays reproducible under a
different seed.
