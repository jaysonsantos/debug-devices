# Test fixtures

Only open files are here. Do not add proprietary boardview files. For a real board, use the local-only test with `BOARDVIEW_TARGET`.

| File | Source | License |
|---|---|---|
| `example.brd` | `example/example.brd` from [whitequark/kicad-boardview](https://github.com/whitequark/kicad-boardview) at `da24793` (2025-02-11). A KiCad example board exported to BRD. | 0BSD, see `LICENSE-0BSD-kicad-boardview.txt` |
| `example.bvr` | `example/example.bvr` from the same repository and commit | 0BSD, see `LICENSE-0BSD-kicad-boardview.txt` |
| `rotation.cad` | Written for this repository: a GenCAD 1.4 file with two resistors (one turned 90 degrees, one on the bottom) | Same license as this repository |

The hooks in `.pre-commit-config.yaml` do not change the files in this directory. They are byte-exact copies.
