# Boardview test fixtures

Only open data is in this directory. Never put a proprietary board file here, or data from one.

| File | Source | License |
|---|---|---|
| `example.brd` | `example/example.brd` from <https://github.com/whitequark/kicad-boardview> (made from a KiCad example board) | 0BSD, see `LICENSE-0BSD.txt` |
| `example.brd.json` | Real output of `obv-dump example.brd` (obv-dump 0.1.0, OpenBoardView 10.0.0, from `nix build .#obv-dump`). `source.path` is changed to `example.brd`, so that no local path is in the repository. OBV reads this file as `brd2`: part boxes are set, pin numbers are empty. | 0BSD (derived from `example.brd`) |
| `tiny.json` | Written by hand for the tests: a set box, rotation, `mfgcode`, bottom and through-hole parts, a test point part, nails, and an unconnected pin. | Same license as this repository |
| `error_*.json` | `DumpError` examples from `docs/boardview-json.md`. `error_unknown_format.json` is real `obv-dump` output (`"format": null`). | Same license as this repository |


To make `example.brd.json` again:

```sh
obv-dump mcp/tests/fixtures/boardview/example.brd \
  | jq -c '.source.path = "example.brd"' > mcp/tests/fixtures/boardview/example.brd.json
```
