# Boardview support: research (Claude)

Date of the check: 2026-09-24. Author: dd-research (Claude Code).

All source facts come from clones in the scratchpad:

- OpenBoardView (OBV) at `cc76e69` (2026-09-05), tag `10.0.0` (released 2026-05-22).
- BoardRipper, wrench-board, `cyrozap/gencad-rs`, `cyrozap/pcbrepair-rs`, `whitequark/kicad-boardview`.

I also did a spike in the scratchpad (not in the repository). It builds only the OBV parsers as a CLI. Section 1.3 has the details.

## Summary

- OBV has **no parser library**. The parsers are inside the single `add_executable` target. But the parsers are easy to separate. They need only a stub `SDL.h` (logging), `utils.cpp`, zlib, `mpc`, and a grammar header that a Python script generates. My spike built them as a 557 KB CLI in about 20 s. It printed JSON for open sample files.
- OBV has **no remote control, IPC, or scripting API**. The only external link is the PDF bridge (Evince D-Bus on Linux, Sumatra DDE on Windows). It searches a PDF for a part name, and it takes a selection from the PDF viewer.
- Other parsers exist, but none is a good dependency for us. BoardRipper (TypeScript, 22 k LOC of parsers) is **AGPL-3.0**. wrench-board (Python, 7.4 k LOC of parsers) is **proprietary source-available**. cyrozap's Rust crates are **GPL-3.0** and cover only FZ/CAE and GenCAD. No package exists on PyPI or npm. FlexBV is closed and has no documented CLI or API.
- All OBV formats give **pin x/y, pin side, and net** in mils. Most give a board outline. The OBV data model does **not** keep the part rotation. Some formats give a part bounding box, and some formats give only pins (the box comes from the pins).
- **Recommendation (option A):** use the OBV parsers (MIT), pinned to a tag, and build them as a small CLI `obv-dump` in `flake.nix`. The Python MCP server runs it as a subprocess and keeps the board in pydantic models. For KiCad boards, use the same model through `kicad-cli` export. The effort is about 4-6 days, and the maintenance is low.

## 1. Can we reuse the OBV parsers as a library?

### 1.1 Code structure

| Path (in `src/openboardview/`) | Purpose | Lines |
|---|---|---|
| `FileFormats/BRDFileBase.h/.cpp` | Common data model: `BRDPoint` (int, **mil**), `BRDPart`, `BRDPin`, `BRDNail`, and `BRDFileBase` with `format` (outline points), `outline_segments`, `parts`, `pins`, `nails` | 240 |
| `FileFormats/*File.cpp/.h` | One parser class per format, each derives from `BRDFileBase`; most have `static bool verifyFormat(buf)` | 4,608 in total for the directory |
| `BRDBoard.cpp/.h`, `Board.h` | Converts a `BRDFileBase` to the GUI model (`Component`, `Pin`, `Net`) | 521 |
| `BoardView.cpp` | GUI. `LoadFile()` (lines 95-150) selects the parser: first by extension (`.fz`, `.cae`, `.asc`/`.bom`, `.cst`), then by `verifyFormat` | 3,589 |
| `Crypto/des.c` | DES for XZZ `.pcb` (from `github.com/dhuertas/DES`) | 368 |
| `utils.cpp/.h` | File helpers. Includes `<SDL.h>` only for `SDL_LogError` | small |

Build: `src/openboardview/CMakeLists.txt` has one `add_executable(${PROJECT_NAME_LOWER} ...)`. It has no `add_library`. The GenCAD grammar header is generated at build time by `utilities/generate_grammar_header.py` from `FileFormats/GenCADFileBnf.h`, and it is parsed with the `mpc` library (submodule `src/mpc`).

Coupling to the GUI:

- `utils.h` includes `<SDL.h>` and uses `SDL_LogError` in the `ENSURE` macro.
- `XZZPCBFile.cpp` and `GenCADFile.cpp` call `SDL_LogWarn`.
- Keys come from the GUI config (`config.FZKey`, `config.CAEKey`, `config.XZZPCBKey`), but the parsers take them as arguments.
- No parser uses ImGui or OpenGL.

License: `LICENSE` says "OpenBoardView is MIT Licensed". GitHub shows `NOASSERTION` because the file has more text. Dependencies: `mpc` is BSD (Daniel Holden), `utf8.h` is public domain, and zlib is under the zlib license. `Crypto/des.c` has no license line in its header. Check `github.com/dhuertas/DES` before a release.

### 1.2 Integration options

| Way | Work | Risk |
|---|---|---|
| **Small CLI that prints JSON** (recommended) | Stub `SDL.h`, one `main.cpp` (about 80 lines, the same dispatch as `BoardView::LoadFile`), a nix derivation. The Python side parses the JSON into pydantic models. | Low. A parser crash kills only the subprocess. |
| pybind11 / nanobind module | Same C++ plus binding code and a Python wheel build for 3.14 in nix | A crash in a parser kills the MCP server (see the segfault in 1.3). Global state in the parsers (`READ_*` macros, arena pointers). |
| ctypes / cffi | Needs a C API on top of C++ classes | Same crash risk. More glue code. |

### 1.3 Spike result (scratchpad only)

I compiled `utils.cpp`, `FileFormats/*.cpp`, `Crypto/des.c`, and `mpc/mpc.c` with a 10-line stub `SDL.h` and a `obv_dump.cpp` main:

```sh
python3 utilities/generate_grammar_header.py \
  src/openboardview/FileFormats/GenCADFileBnf.h gen/build-generated/GenCADFileGrammar.h
g++ -std=c++17 -O1 -DWITH_STD_FILESYSTEM -I include -I gen -I src/openboardview -I src -I $ZLIB_DEV/include \
  obv_dump.cpp src/openboardview/utils.cpp src/openboardview/FileFormats/*.cpp \
  src/openboardview/Crypto/des.c -x c src/mpc/mpc.c -x none -lz -o obv-dump
```

Result: exit 0, one warning, a 557 KB binary, about 20 s CPU time.

Runs on open sample files:

| Sample (public repository) | Result |
|---|---|
| `whitequark/kicad-boardview/example/example.brd` (made from KiCad, 0BSD) | OK: 247 parts, 1,149 pins (1,130 with a net), outline with 73 points |
| `whitequark/kicad-boardview/example/example.bvr` | OK: 272 parts, 1,149 pins, outline with 73 points, pin numbers and nets such as `/SCL` |
| `BoardRipper/.../samples/test-board.bvr` | OK: 10 parts, 38 pins, outline with 4 points |
| `cyrozap/gencad-rs/tests/fixtures/example.cad` (the GenCAD 1.4 specification example) | Fails: "the $SHAPES section was not parsed properly". The OBV grammar is strict. |
| `BoardRipper/.../samples/multilayer-test.cad` | **Segmentation fault** (exit 139) after GenCAD errors |

Conclusion: the parsers work without the GUI. The segfault is a strong reason to use a subprocess, not a Python extension.

## 2. Remote control, IPC, CLI, or scripting in OBV

- Command line (`main_opengl.cpp`, `help[]` and `parse_parameters`): `-h`, `-V`, `-l` (slow CPU), `-c <config>`, `-i <input file>`, `-x/-y` (window size), `-z` (font size), `-p` (DPI), `-r` (renderer), `-d` (debug). A single file argument opens the file. There is **no** option to select or highlight a part or a net.
- `--reversesearch <PDF path> <search string>` exists only on Windows (`#ifdef _WIN32`). It is for the Sumatra PDF DDE bridge.
- PDF bridge (`PDFBridge/`): on Linux, OBV connects to Evince over D-Bus (`org.gnome.evince.Daemon`, `org.gnome.evince.Window`). It sends `Search` to Evince for the selected part (`BoardView.cpp:356`). It reads the Evince `SelectionChanged` signal and then searches the board (`HandlePDFBridgeSelection`, `BoardView.cpp:3578`). Thus, a program that acts as Evince on D-Bus can make OBV search a part. This is a hack. Do not use it.
- No plugin or scripting API. No socket. Open issues ask for other things: #299 "Export view to PNG" (2025), #11 "Custom file format / wrapper" (2016), #146 "KiCAD Boardview exporter" (2018), #167 "ODB++ Support" (2022).
- A fork with a small socket (for example, a Unix socket with the commands `open`, `select_part`, `select_net`, `flip`, `screenshot`) is possible. `BoardView` already has functions for search and selection. The effort is about 3-5 days. The maintenance cost is a merge after each upstream release. Upstream activity is low: 84 non-merge commits in the last 3 years in `src/openboardview`, 3 releases (9.95.1 2024-07-24, 9.95.2 2025-08-19, 10.0.0 2026-05-22). Do this only if the user wants to look at a live OBV window that the agent controls. For the agent, an image that we make from our own model gives the same information (section 7).

## 3. Other libraries and tools

| Project | Language | License | Formats | Activity | Use for us |
|---|---|---|---|---|---|
| [AlexeyInwerp/BoardRipper](https://github.com/AlexeyInwerp/BoardRipper) | TypeScript (web, Electron) | **AGPL-3.0** | BVR1/3, BRD, BDV, BDV ASC, FZ, GenCAD, Mentor Neutral, XZZ, TVW, Allegro v15-v18 (derived from KiCad, GPL), Altium, KiCad, EAGLE | Created 2026-03, 1,964 commits, pushed 2026-09-23, 29 stars | Largest format list. Parsers are 22,307 LOC in `src/frontend/src/parsers/`, and its data model keeps rotation, pads, and side. Its AGPL license and browser/worker packaging make it a poor library. Use it as a reference and as a manual viewer. |
| [Junkz3/wrench-board](https://github.com/Junkz3/wrench-board) | Python + Rust | **Proprietary source-available** ("All rights reserved") | BRD (Test_Link), BRD2, BDV, ASC, BV, BVR, CAD, CST, FZ, GR, TVW, F2B, XZZ, GenCAD, KiCad | Created 2026-04, 16 commits, pushed 2026-07-20, 239 stars | Do not copy code. Its agent tools in `api/tools/boardview.py` are a good design reference: `highlight_component`, `focus_component`, `highlight_net`, `flip_board`, `annotate`, `measure_distance`, `show_pin`, `dim_unrelated`. |
| [cyrozap/pcbrepair-rs](https://github.com/cyrozap/pcbrepair-rs) (crate `pcbrepair` 0.4.1) | Rust | GPL-3.0 | ASUS `.fz`, ASRock `.cae` | 2026-01 to 2026-03 | Narrow. GPL. |
| [cyrozap/gencad-rs](https://github.com/cyrozap/gencad-rs) (crate `gencad` 0.2.1) | Rust | GPL-3.0 | GenCAD | 2026-01 to 2026-03 | Narrow. GPL. |
| [whitequark/kicad-boardview](https://github.com/whitequark/kicad-boardview) | Python | 0BSD | Writes BRD (Test_Link) and BVR from KiCad | pushed 2025-02, 182 stars | Useful for the KiCad path (section 5). Its output parses in the OBV spike. |
| Thermetery-Technology-LLC/thermetery-boardview | Python | LGPL-3.0 | TVW | 2026-05 to 2026-08, 8 stars | Beta, one format. |
| FlexBV 5 ([manual](https://pldaniels.com/flexbv5/manual/flexbv-manual.html)) | closed | commercial | XZZ, BRD, TVW, GenCAD, FZ, BVR, KiCad, Eagle, Allegro, EasyEDA Pro, Samsung CAD | active | The manual has no CLI parameters, no API, no socket, and no export. Not usable for automation. |
| PyPI | - | - | - | - | No packages named `boardview`, `openboardview`, `pyboardview`, `brdfile`, or `gencad` (HTTP 404). |
| npm | - | - | - | - | No boardview parser. The results for "boardview" are other things (CRM, sticky notes). |

## 4. Physical positions per format

OBV normalizes all formats to integer **mil** (1/1000 inch) in `BRDPoint`. The table shows what each OBV parser fills. "Outline" means that the parser fills `format` (points) or `outline_segments`. "Rotation" means that the parser reads rotation or angles to place pins. The rotation is not stored in the output.

| Format (OBV class) | Extension | Pin x/y | Pin/part side | Net per pin | Part box | Outline | Rotation in source | Notes |
|---|---|---|---|---|---|---|---|---|
| Test_Link BRD (`BRDFile`) | `.brd` | yes | yes | yes | from pins | points | no | Tested (kicad-boardview example) |
| BRD2 (`BRD2File`) | `.brd` | yes | yes | yes | yes (`p1/p2`) | points | no | |
| BDV (`BDVFile`) | `.bdv` | yes | yes | yes | from pins | points | no | Obfuscated text |
| BVR (`BVRFile`) | `.bvr`, `.bv` | yes | yes | yes | from pins | points | no | Tested |
| BVR3 (`BVR3File`) | `.bvr` | yes (relative to part since 2026-01) | yes | yes | from pins | segments | no | |
| ASUS TSICT (`ASCFile`) | `.asc`, `.bom` | yes | yes | yes | from pins | points | no | Several sibling files |
| Samsung CAD (`CADFile`) | `.cad` | yes | yes | yes | from pins | points | no | |
| CST (`CSTFile`) | `.cst` | yes | yes | yes | from pins | fake outline from outer pins plus a margin | no | |
| ASUS PCBRepair (`FZFile`) | `.fz` | yes | yes | yes | from pins | points | yes | RC6 + zlib. Needs a key. |
| ASRock PCBRepair Pro (`CAEFile`, subclass of FZ) | `.cae` | yes | yes | yes | from pins | points (FZ parser) | yes (FZ parser) | Needs its own key. Only the key and the parity differ from FZ. |
| GenCAD 1.4 (`GenCADFile`) | `.cad` | yes | yes | yes | yes | segments | yes | Strict grammar (the spec example fails) |
| Altium/Protel ASCII (`ADFile`) | `.pcbdoc` (ASCII only, `\|KIND=Protel_Advanced_PCB`) | yes | yes | yes | from pins | points and segments | yes | Binary PcbDoc is refused |
| XZZ (`XZZPCBFile`) | `.pcb` | yes | yes | yes | from pins | segments | yes | DES. Needs a key. Two sides are side by side ("butterfly"). |
| Allegro (`BRDAllegroFile`) | `.brd` | - | - | - | - | - | - | Not supported. OBV shows "use Allegro FREE Physical Viewer". |
| Teboview TVW | `.tvw` | - | - | - | - | - | - | Not in OBV (BoardRipper, wrench-board, and FlexBV read it) |

What we can compute:

- The part center is the mean of its pins, or the center of `p1/p2` when it is set.
- The approximate part rotation comes from the pin 1 position against the center, or from the long axis of the pin cloud. It is exact only when the source format has rotation (GenCAD, XZZ, Altium ASCII, FZ), and OBV discards it. A small patch (one field in `BRDPart`) can keep it.
- Bottom side: mirror X (or use the side flag) before the drawing or the photo mapping.

### Mapping to a phone photo

Yes, this is possible, and it is simple:

1. The board is flat. Thus, a **homography** (3x3, 8 degrees of freedom) maps board mils to photo pixels for one side. It needs at least **4** point pairs. Three pairs give only an affine transform. That is sufficient when the phone is almost parallel to the board.
2. Point pairs: centers of 4-6 large, well-spread parts (ICs, connectors, mounting holes) that the user or the vision model finds in the photo. Use `cv2.findHomography(board_pts, photo_pts, cv2.RANSAC)` (package `opencv-python-headless`).
3. Check: compute the reprojection error on one more part. Refuse the mapping when the error is more than about 2 % of the board size.
4. Limits: lens distortion at wide angle (use zoom 1.5-2x on the phone), tall parts (their top is not at board height), and the other side of the board needs its own homography with mirrored X.
5. After the mapping, `locate_in_photo(refdes | net)` gives pixel positions, and the tool can draw circles on the phone snapshot.

## 5. Real design data: KiCad, IPC-2581, ODB++, Gerber + pick-and-place

| Source | What it has | Tool | Assessment |
|---|---|---|---|
| **KiCad** `.kicad_pcb` | Everything: footprints with x/y/rotation/side, pads, nets, outline, silkscreen | `kicad-cli` (KiCad 10.0.6 in nixpkgs). `pcb export pos` (CSV, `--units mm`, `--side both`), `pcb export ipcd356` (netlist with pad x/y), `pcb export gencad`, `ipc2581`, `odb`, `svg`. `kicad-python` 0.8.0 (IPC API, needs a running KiCad). `kiutils` 1.4.8 (file parser, last release 2024-02). | **Best base when the design exists.** `pos` + `ipcd356` give parts with rotation, pads with nets, and positions in mm. No reverse engineering is necessary. |
| IPC-2581 (B/C) | Full XML: parts, rotation, pads, nets, layers, outline | KiCad and Altium export it. No maintained Python parser on PyPI (`ipc2581`: 404). | Good and open, but we must write an XML reader (1-2 days for the subset we need). |
| ODB++ | Full data, directory tree of text files | KiCad/Altium/Allegro export it. No parser on PyPI (`odbpp`: 404). | More complex than IPC-2581. No advantage for us. |
| Gerber + pick-and-place CSV | Gerber: copper and silkscreen graphics only, no parts. CSV: refdes, x, y, rotation, side. IPC-D-356 (if present): nets and pad positions. | `gerbonara` 1.6.3 (2026-04), `pygerber` 2.4.3 | CSV alone gives part positions without nets. CSV + IPC-D-356 gives almost everything. Gerber adds only a picture. |

Conclusion: for the user's own boards, use KiCad through `kicad-cli pcb export pos` and `pcb export ipcd356` into the same pydantic model as the boardview files. Do not use the KiCad → GenCAD → OBV path. The OBV GenCAD grammar is strict, and I did not test it with KiCad output. `whitequark/kicad-boardview` (0BSD) is a second way: it writes a BRD that parses in the spike.

## 6. Check of the claim "parsing CAD files ourselves is expensive and hard to maintain"

The claim is **true for a new parser**. It is **false for the OBV parsers**, because they are small and stable.

Size (OBV `src/openboardview/FileFormats`, lines including headers; BoardRipper for comparison):

| Format | OBV lines (.cpp) | BoardRipper lines (TS parser) | Commits in OBV since 2023-09-24 / total |
|---|---|---|---|
| GenCAD (+ BNF grammar 175 lines) | 762 | 1,725 (`cad-parser.ts`) | 9 / 30 (+ 7 / 18 for the grammar) |
| XZZ | 525 | 2,866 | 2 / 2 (added 2025-08-15, "v3") |
| Altium/Protel ASCII (AD) | 495 | 1,652 (Altium directory, binary and ASCII) | 0 / 10 |
| FZ | 488 | 526 | 5 / 26 |
| BVR3 | 238 | 214 | 1 / 10 |
| BRD2 | 234 | - | 0 / 12 |
| ASC | 181 | 487 (BDV ASC) | 0 / 12 |
| CST | 167 | - | 0 / 6 |
| BVR | 166 | - | 0 / 10 |
| CAD (Samsung) | 165 | - | 0 / 17 |
| BRD (Test_Link) | 161 | 366 | 0 / 10 |
| BDV | 154 | 395 | 0 / 8 |
| CAE | 16 (+ FZ) | - | 2 / 2 (added 2025-01-08) |
| **All of `FileFormats/`** | **4,608** | 22,307 (all parsers) | **23 non-merge commits in 3 years** |

Change history in the last 3 years (non-merge, `FileFormats/`): 16 of the 23 commits are GenCAD fixes for badly formed files (for example 2024-07-14 "some poorly-made files have an INSERT keyword...", 2026-05-23 "fixup deduplicating elements placed on opposite sides"). The rest: XZZ v3 support (2025-08-15), CAE support (2025-01-08), FZ multiple built-in keys (2025-08-19), FZ/CAE side fix (2026-03-07), BVR3 relative pin coordinates (2026-01-03). The simple text formats (BRD, BDV, BVR, CAD, CST, ASC, BRD2) had **no** change.

Known quirks:

- **FZ (ASUS):** RC6 encryption, then zlib. It needs a 44-word key. OBV ships **no** key: `FZFile::getBuiltinKey()` and `CAEFile::getBuiltinKey()` return `{}`. OBV checks the parity of the user key (`getKeyParity()`). The user sets `FZKey` in `obv.conf`.
- **CAE (ASRock):** the same scheme as FZ with its own key (`CAEKey`).
- **XZZ (`.pcb`):** DES with a 64-bit key (`XZZPCBKey`) with a parity check. It has several versions (v3 in 2025). The FlexBV manual says that XZZ files are "pre-split" (both sides are side by side in one drawing).
- **BDV:** simple obfuscation of the text (no key): each byte is `key - byte`, and the key starts at `0xA0` and changes on each line (`decode_bdv` in `BDVFile.cpp`).
- **ASC:** the board is split into several files in one directory. `ASCFile` finds the sibling files with `lookup_file_insensitive`.
- **GenCAD:** many exporters make invalid files. OBV has special cases. The spec example fails.
- **Allegro binary `.brd`:** not supported by OBV. It has the same `.brd` extension as Test_Link, and OBV detects it at offset 0xF8.
- **Detection:** `.fz`, `.cae`, `.asc/.bom`, and `.cst` are selected by extension. The other formats are selected by content.

Legal: we must not ship decryption keys. The MCP reads the keys from `.env` / flags (for example `BOARDVIEW_FZ_KEY`, `BOARDVIEW_XZZ_KEY`) that the user supplies. I did not download proprietary boardview files. All tests used open samples.

## 7. Recommendation

### Option A (recommended): OBV parsers as a pinned CLI, board model and tools in Python

- Build: a nix derivation in `flake.nix` that fetches OBV at tag `10.0.0` (`fetchFromGitHub` with submodules `mpc` and `utf8`) and compiles `obv-dump` from our small `main.cpp` + stub `SDL.h`. The C++ files live in a new `boardview/` directory (C++ only). Optional small patch: keep the part rotation in the JSON.
- `obv-dump FILE [--fz-key ...] [--xzz-key ...]` prints JSON: units (mil), outline, parts (name, side, box), pins (part, x, y, side, net, number, name, radius), nails.
- The MCP server runs it with a timeout, validates the JSON with pydantic, converts it to mm, computes part centers, and caches the result by file hash.
- The KiCad path goes into the same model: `kicad-cli pcb export pos --format csv --units mm` + `kicad-cli pcb export ipcd356`.
- Pictures: the server draws the board with Pillow or SVG (outline, part boxes, highlighted part/net) and returns an image. No GUI is necessary.

Effort: about **4-6 days**. That is 1 day for the nix build and the CLI, 1-1.5 days for the Python model and tools, 1 day for rendering, 1 day for photo mapping, and 0.5-1 day for tests with open samples.

Maintenance: **low**. Bump the OBV tag about once per year (3 releases in 3 years). Most upstream changes are GenCAD fixes that we get for free. Our own code is about 150 lines of C++ and the Python tools.

Risks:

- Parser crashes on bad files (seen: segfault). The subprocess and a timeout contain them.
- Formats that OBV does not read: TVW, Allegro binary, EasyEDA Pro. For these, the user can convert the file with another viewer, or we add a parser later.
- Encrypted formats need user keys.
- Rotation is lost without the small patch.
- `Crypto/des.c` has no license line. Check it before we publish binaries.

### Option B (only if a live viewer is necessary): fork OBV and add a control socket

- A fork that adds a Unix socket with `open`, `select_part`, `select_net`, `flip`, `zoom_to`, and `screenshot`. The MCP server drives the real OBV window.
- Effort: **3-5 days** in addition to option A. OBV has no screenshot export (issue #299), so that command is also new code.
- Maintenance: **medium**. Rebase on each release, keep the GUI build (SDL2, ImGui, GTK) in nix, and maintain the fork.
- Use it only if the user wants to look at the board in OBV while the agent points at parts. Option A already gives images to the agent and to the user.

## 8. Proposed MCP tools (option A)

| Tool | Input | Output |
|---|---|---|
| `board_load` | `path`, optional `side_hint` | Summary: format, units, board size (mm), part/pin/net counts, sides |
| `board_find_part` | `query` (refdes, glob, or value/`mfgcode`) | Parts: refdes, side, center (mm), box, pin count, nets |
| `board_part_pins` | `refdes` | Pins: number, name, net, x/y (mm), side |
| `board_find_net` | `net` (name or glob) | Pins and parts on the net, test points/nails, the nearest test point to each part |
| `board_parts_near` | `refdes` or `x`/`y`, `radius_mm`, `side` | Parts in the radius, sorted by distance |
| `board_render` | `side`, `highlight_parts`, `highlight_nets`, `crop_to` | PNG image plus the legend as structured JSON |
| `board_register_photo` | `side`, 4-6 pairs `{refdes, pixel_x, pixel_y}` (or a phone snapshot plus the refdes list for the vision model) | Homography id, reprojection error |
| `board_locate_in_photo` | `refdes` or `net`, `homography_id`, optional new phone snapshot | Pixel positions and an annotated JPEG |

The tools use the same pattern as the phone tools: pydantic models, `Annotated[CallToolResult, Model]` for image plus JSON (see `docs/research.md` section 2), and config through flags and environment variables (`BOARDVIEW_DUMP_BIN`, `BOARDVIEW_FZ_KEY`, `BOARDVIEW_CAE_KEY`, `BOARDVIEW_XZZ_KEY`, `KICAD_CLI_BIN`).

## Sources

- OpenBoardView: <https://github.com/OpenBoardView/OpenBoardView> (commit `cc76e69`, 2026-09-05; files named above).
- BoardRipper: <https://github.com/AlexeyInwerp/BoardRipper> (`LICENSE`, `THIRD_PARTY.md`, `src/frontend/src/parsers/`).
- wrench-board: <https://github.com/Junkz3/wrench-board> (`LICENSE`, `api/board/parser/`, `api/tools/boardview.py`).
- pcbrepair-rs: <https://github.com/cyrozap/pcbrepair-rs>. gencad-rs: <https://github.com/cyrozap/gencad-rs>.
- kicad-boardview: <https://github.com/whitequark/kicad-boardview>.
- FlexBV manual: <https://pldaniels.com/flexbv5/manual/flexbv-manual.html>.
- KiCad CLI: <https://docs.kicad.org/10.0/en/cli/cli.html>. kicad-python on PyPI: <https://pypi.org/project/kicad-python/>.
- crates.io search `boardview` and `gencad`, npm search `boardview`, PyPI JSON API (all on 2026-09-24).
