# Repair the broken build target

`Makefile` in the working directory defines a `report` target that should build
`report.txt` by sorting the lines of its input file. The target is currently
broken and `make report` fails.

Fix `Makefile` so that running `make report` succeeds and produces
`report.txt` containing the input lines in sorted order, one per line. Keep
the existing target names (`report`); the verifier runs `make report` and
inspects the artifact it produces.
