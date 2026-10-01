# FastHenry 3.0.1: a port spanning disconnected conductors is NOT always refused

Reproduction decks for `~/dev/kb/tooling/fasthenry-ports-across-disconnected-conductors.md`.
Run with FastHenry 3.0.1 (28May12, FastFieldSolvers), 2026-09-16.

The `.inp` and `.mat` files here are `git add -f`-ed past `.gitignore` (which
excludes `*.inp` and `*.mat` as run output). They are not run output: they are the
evidence for a verified kb claim, and without them the note's `repo:` locator
points at a directory with the conclusions and none of the data.

## `split.inp` — ONE port across two separate wires → hard error

Two parallel 5 mm wires, nothing joining them, one `.external` from one to the
other. `split.fasthenry.log`:

    Number of meshes:             0
    Couldn't create sparse matrix, err 5

exit 1, no `Zc.mat` produced. This is the response the extractor's error messages
quote, and it is what you get for the single-port case only.

## `twoport-disconnected.inp` — TWO ports across the SAME separation → solves

Real deck: `examples/buck-tpsm33610-module.yaml` at mesh pitch 0.35 mm with
`validate_module_ports` bypassed. At that pitch the GND pour mesh under U1's
ground pad forms a 22-node island, so both `P_pwr` (C2) and `P_pwr1` (C1) span
island <-> main plane. The two source branches close a fundamental circuit
between the two components, so FastHenry reports 1272 meshes, writes `Zc.mat`,
and exits 0 (`twoport-disconnected.fasthenry.tail.log`).

`twoport-disconnected.Zc.mat` reduces at the 3.9 MHz plateau to

    L = 4.32e+16 nH   (all four entries identical)
    R = 2.65e+17 mOhm (all four entries identical)

i.e. a clean exit and a finite matrix that measured nothing. This is why
`kicad_geom.validate_module_ports` checks port connectivity BEFORE the solve
rather than trusting FastHenry's exit status.
