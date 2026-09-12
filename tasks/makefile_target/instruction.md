# Repair the broken build target

`Makefile` in the working directory defines a `report` target that should build
`report.txt` by sorting the lines of its input file. The target is currently
broken and `make` fails.

Fix `Makefile` so that running `make report` succeeds and produces
`report.txt` containing the input lines in sorted order, one per line.

The recipe must read the input file declared by the Makefile (either the
`$(INPUT)` variable or `input.txt`); do not hardcode the output or copy a
prepared file into place. The verifier deletes any existing `report.txt`, runs
`make -B report` (forcing the recipe to run), and checks both the regenerated
contents and that the recipe references the input.
