# boardview

`obv-dump` reads a boardview file with the [OpenBoardView](https://github.com/OpenBoardView/OpenBoardView) parsers and writes JSON to stdout. The MCP server runs it as a subprocess. The JSON contract is `docs/boardview-json.md`.

## Use

```sh
obv-dump board.cad > board.json
obv-dump board.fz --fz-key "0x..., 0x..., ..."   # 44 hex words
obv-dump --version
```

- Exit 0: `BoardDump` on stdout. Exit 1: `DumpError` on stdout. Exit 2: bad command line.
- Log text goes to stderr.
- Keys come only from the command line. `obv-dump` has no built-in keys.

## Build

The dev shell has `obv-dump` on `PATH`. `flake.nix` builds it:

1. It fetches OpenBoardView at tag `10.0.0` and the `mpc` and `utf8` submodules at the revisions of that tag.
2. It applies the patches in `patches/`.
3. It compiles only the parsers (`src/openboardview/FileFormats`, `utils.cpp`, `Crypto/des.c`, `mpc.c`) with `src/main.cpp` and the stub `include/SDL.h`. The parsers use SDL only for log output.

```sh
nix build .#obv-dump
```

A clean build takes about 10 seconds.

## Patches

- `0001-keep-part-rotation.patch`: adds `has_rotation` and `rotation_deg` to `BRDPart`. The GenCAD, FZ, and Altium ASCII (`ad`) parsers set them. The other parsers do not read a rotation, so `rotation_deg` is `null`.

To change a patch: check out the tag, apply the patches, make the change, and write `git diff` to the patch file. The hooks do not change the files in `patches/`.

## Tests

```sh
nix develop --command boardview/tests/run.sh
BOARDVIEW_TARGET=/path/to/local/board.cad nix develop --command boardview/tests/run.sh
```

## Licenses

- OpenBoardView: MIT, see `LICENSE.OpenBoardView`.
- `mpc` (Daniel Holden, 2013): BSD 2-Clause.
- `utf8.h` (sheredom): public domain (Unlicense text).
- `Crypto/des.c` in OpenBoardView, from [dhuertas/DES](https://github.com/dhuertas/DES): MIT (Dani Huertas, 2020).
- Test fixtures: see `tests/fixtures/README.md`.

`nix build .#obv-dump` installs these license texts in `share/licenses/obv-dump/`.
