# Boardview support for the debug-devices MCP

Check date: 2026-09-24.

OpenBoardView source: commit `cc76e697` on `master`, 2026-09-05. Release 10.0.0, 2026-05-22. CMake sets `project(OpenBoardView VERSION 10.0.0)`. License: MIT (`LICENSE` in the repository).

https://github.com/OpenBoardView/OpenBoardView

## Recommendation

Use option 1.

**Option 1 (first).** Vendor the OpenBoardView parsers. Build a small C++ program. The program reads one boardview file and writes JSON to stdout. The Python MCP server starts this program as a subprocess. Do not link the parsers into Python with pybind11, ctypes, or cffi.

Effort: 6 to 8 days. This time includes the JSON program, tests on open sample files, and the MCP tools in the list below.

Maintenance: low for the old text formats. Those files had 0 commits in `FileFormats/` since 2023-09-24. Plan a few hours when an upstream format fix is necessary. In the last 3 years, `FileFormats/` had 19 commits. Most of those commits are GenCAD, FZ, XZZ, and CAE.

**Option 2.** When the user has a KiCad board, call `kicad-cli`. Do not parse `.kicad_pcb`, IPC-2581, or ODB++ in this repository. This option does not replace option 1. Repair boardview files are not KiCad files.

Do not fork OpenBoardView to add a socket. The viewer is a GUI. A socket can highlight a part on the screen. It does not give the agent a coordinate for the phone camera.

## 1. Library or application code

The parsers are not a library. `src/openboardview/CMakeLists.txt` has one target: `add_executable(openboardview ...)`. There is no `add_library` for the parsers. The other libraries in the tree are imgui, SQLite, mpc, and glad. Those libraries are for the GUI.

The code layout is:

- `src/openboardview/FileFormats/` holds one parser class per format. Each class extends `BRDFileBase` (`FileFormats/BRDFileBase.h`).
- `BRDFileBase` stores outline points, parts, pins, and nails.
- `BRDBoard` (`BRDBoard.cpp`) copies that data into `Component`, `Pin`, and `Net` objects (`Board.h`).
- `BoardView::LoadFile` (`BoardView.cpp`) selects the parser. The selection uses the file extension for `.fz`, `.cae`, `.asc`, `.bom`, and `.cst`. Other formats use `verifyFormat()`.

A shared library is possible, but the parsers are not library code:

- Many parsers call `setlocale(LC_NUMERIC, "C")`. That call changes the process locale.
- `ADFile.cpp` uses global pointers `arena` and `arena_end`.
- `XZZPCBFile.cpp` calls `SDL_LogWarn`. The XZZ parser needs SDL.
- GenCAD needs the mpc parser and a generated grammar header (`utilities/generate_grammar_header.py`).
- FZ and CAE need zlib. XZZ needs the DES code in `Crypto/des.h`.
- Strings are raw pointers into a temporary buffer. The caller must keep that buffer alive.

ctypes or cffi need a stable C API. That API does not exist. pybind11 can wrap the C++ classes, but the locale change and the globals remain.

A JSON command-line program is the small interface. The MCP process does not link C++. The JSON program can vendor `FileFormats/` under the MIT license. Put the copyright notice in the vendored tree.

## 2. Remote control

OpenBoardView has no plugin API, no script API, and no socket.

`src/openboardview/main_opengl.cpp` accepts these flags: `-h`, `-V`, `-l`, `-c`, `-i`, `-x`, `-y`, `-z`, `-p`, `-r`, `-d`. `-i` loads one board file into the window. A single path argument does the same load. On Windows, `--reversesearch` is a DDE command for SumatraPDF. It is not a board query.

Search and highlight are methods on `BoardView`: `FindComponent`, `FindNet`, `SearchPartsAndNets` (`BoardView.h`). The GUI calls them. No other process can call them.

The only IPC is the PDF bridge. Windows uses DDE with SumatraPDF. Linux uses D-Bus with Evince. The bridge sends a search string to a PDF viewer. It does not accept a part name from another program.

A socket fork is a new feature in a large SDL and ImGui program. The program needs a display. The agent still needs coordinates in a file, not a highlight on a screen. Do not do this work.

## 3. Other parsers and viewers

No PyPI package parses boardview files. A search for boardview and FZ parsers on PyPI on 2026-09-24 did not find one. `kiutils` parses KiCad files, not boardview files. https://pypi.org/project/kiutils/

No npm package parses boardview files. npm has Gerber parsers (`gerber-parser`, `@tracespace/core`). Those parsers read Gerber and drill files.

| Tool | Language | License | Formats | Activity |
|---|---|---|---|---|
| OpenBoardView 10.0.0 | C++ | MIT | See section 4. Allegro binary is rejected. | Push 2026-09-05. About 1830 stars. |
| `pcbrepair` 0.4.1 | Rust library | GPL-3.0-or-later | ASUS `.fz` and ASRock `.cae` only | crates.io. Created 2026-01-05. Version 0.4.1 on 2026-03-07. About 157 downloads. https://github.com/cyrozap/pcbrepair-rs |
| BoardRipper v0.31.17 | TypeScript and Go, web viewer | AGPL-3.0 | BVR1, BVR3, BRD, BDV, BDV ASC, FZ, GenCAD, Mentor Neutral, XZZ, TVW, Allegro v15 to v18 | Created 2026-03-19. Release 2026-06-08. https://github.com/AlexeyInwerp/BoardRipper |
| `boardview-tools` | C | MIT | BRD and BDV decode | Last push 2020-01-19. https://github.com/nitrocaster/boardview-tools |
| `kicad-boardview` | Python | (see repository) | Writes `.brd` and `.bvr` from KiCad. It does not parse boardview files. | https://github.com/whitequark/kicad-boardview |

`pcbrepair` is a real library, but it covers two formats. The license is GPL-3.0-or-later. Do not link it into the MCP server.

BoardRipper is a viewer. The README shows Docker and a browser. It does not show a JSON command for an agent. The license is AGPL-3.0 because the Allegro parser comes from KiCad. The project is new. Do not make the MCP server depend on it.

FlexBV is a commercial viewer from the same author history as OpenBoardView (Paul Daniels). The old help text matches OpenBoardView: `-i` loads a file, and the other flags set the window. https://pldaniels.com/flexbv/installation.html

FlexBV5 latest listed build: v5.3162 on 2026-07-27. The changelog names GenCAD repairs and schematic-viewer commands. It does not name a board query API. https://pldaniels.com/flexbv5/

OpenBoardView rejects Cadence Allegro binary `.brd` files. `BRDAllegroFile.h` sets `valid = false` and tells the user to use Allegro FREE Physical Viewer. BoardRipper and ZenPCBParser parse Allegro. That work is a different parser, and it is large. Do not add it in the first version.

## 4. Physical positions

The internal unit is the mil (one thousandth of an inch). `BRDPoint` in `BRDFileBase.h` says "mil (thou) is used here". Coordinates are integers.

`Component` in `Board.h` has no rotation field. The part box is `p1` and `p2`. Many parsers leave `p1` and `p2` at 0, 0. The part location is then the center of its pins.

| Format | Parser | Part x/y | Rotation | Side | Pin x/y | Outline | Unit note |
|---|---|---|---|---|---|---|---|
| Landrex / TestLink `.brd` | `BRDFile.cpp` (161 lines) | No body box | No | Yes, from a type byte | Yes | `Format:` points | Integers are already mils. Bytes with header `23 e2 63 28` are decoded by a bit rotate. |
| ASCII `.brd` (BRD2) | `BRD2File.cpp` (234) | Yes, `p1` and `p2` | No | Yes | Yes | `BRDOUT:` points | Integers. |
| `.bdv` | `BDVFile.cpp` (154) | No | No | Yes | Yes | `<<format.asc>>` | File numbers times 1000. The parser skips 8 lines after some headers. |
| `.asc` / `.bom` | `ASCFile.cpp` (181) | No | No | Yes | Yes | Format block | Same times-1000 scale. Extension selects the parser. |
| `.bvr` format 1 | `BVRFile.cpp` (166) | No | No | Yes | Yes | Format block | Times 1000. Signature `BVRAW_FORMAT_1`. |
| `.bvr` format 3 | `BVR3File.cpp` (238) | `PART_ORIGIN` is in `BVR3Part.pos`, not in `p1` | No | `PART_SIDE` | Yes. `PIN_ORIGIN` can be relative to the part. | `OUTLINE_POINTS` | `PART_OUTLINE_RELATIVE` is ignored. Commit `cc1660169`, 2026-01-03. |
| `.cad` (not GenCAD) | `CADFile.cpp` (165) | No | No | Yes | Yes | Built in `gen_outline()` | Times 1000. |
| GenCAD `.cad` | `GenCADFile.cpp` (762) plus grammar | Yes, place x/y in `p1` and `p2` | Used to place pins. Not stored on the part. | TOP or BOTTOM | Yes, after rotation | Board outline, or a rectangle, or arcs | Converted to mils in `board_unit_to_brd_coordinate`. |
| Altium ASCII | `ADFile.cpp` (495) | Parsed (`X`, `Y`, `ROTATION`) then not copied to `p1` | On the Altium record only | TOP or BOTTOM | Yes, pad X and Y | Track and arc records | Signature `KIND=Protel_Advanced_PCB`. Binary Altium is rejected. |
| `.fz` (ASUS) | `FZFile.cpp` (488) | No. `srotate` is read and discarded. | Discarded | Mirror YES means bottom | Yes | Rectangle around the pins, margin 20 mil | Default unit is thou. `UNIT:millimeters` multiplies by 25.4. |
| `.cae` (ASRock) | `CAEFile.cpp` (16) | Same as FZ | Same as FZ | Side fix in 2026-03-07 | Same as FZ | Same as FZ | Same pipeline. Different key check. |
| `.cst` | `CSTFile.cpp` (167) | No | No | Yes | Yes | Fake rectangle around pins | Comment: outline is not known. |
| XZZ `.pcb` | `XZZPCBFile.cpp` (525) | No body box | No | Set to top | Yes | Segments, including arcs | DES, then integer scale. |
| Allegro `.brd` | `BRDAllegroFile.h` (17) | No | No | No | No | No | Detected at offset `0xf8` (`all` or `vie`). Not parsed. |

A phone photo can use a homography later. The board is a plane. Four reference parts that are not on one line give the map from board mils to photo pixels. Three points are the minimum for an affine map. Four points are the better set for a projective map.

Use this procedure:

1. Read pin coordinates, or `p1` when the parser sets it.
2. Compute the part center from the pins when `p1` is 0, 0.
3. The user marks the same parts in one phone photo.
4. Solve the homography. Apply it to other parts on the same side.

Limits: one photo shows one side. A close lens bends straight lines. The board must be flat in the photo. Formats with no real outline (FZ, CST) give a pin box, not the board edge. Part rotation is missing in the common model, except where GenCAD already moved the pins.

## 5. Real design data

When the user has design files, use the design files for positions. Boardview files are the repair exchange format. They have parts, pins, and nets. They often have no part body and no stored rotation.

KiCad is the better base for a board that KiCad made.

- `kicad-cli pcb export pos` writes a position file. Flags include `--format csv`, `--units mm`, and `--side both`. Each row has the reference, the x position, the y position, the rotation, and the side. https://docs.kicad.org/master/en/cli/cli.html
- The same CLI exports IPC-2581 (`pcb export ipc2581`) and ODB++ (`pcb export odb`).
- KiCad 9 and 10 IPC API does not export files. The KiCad 11 IPC API can run headless. For this project, call `kicad-cli`. Do not import the old `pcbnew` Python module. https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/

IPC-2581 is one XML file. It can hold the outline, the parts, the rotation, the side, and the nets. A full parser is larger than the stable boardview parsers. Call `kicad-cli` when the source is KiCad. Accept an IPC-2581 file only when a fab tool already wrote it, and parse only the part and net records that you need.

ODB++ is a directory of layer files. It has part positions. It is a poor first format for a hand parser.

Gerber files have copper shapes and a board edge. They do not have reference designators or net names. A pick-and-place CSV has reference, x, y, rotation, and side. It does not have nets. Gerber plus CSV can point the camera at a part. It cannot answer "which pins are on this net".

Order of sources for one board:

1. KiCad board, through `kicad-cli`, when the user has the design.
2. Boardview file, through the JSON program, when the user has a repair file.
3. Pick-and-place CSV, for positions only, when the other files are absent.

## 6. The maintenance claim

The claim is half true.

`FileFormats/` is 4608 lines (`wc -l` on commit `cc76e697`). The common model (`BRDBoard.cpp` and `Board.h`) is about 490 lines more.

Lines are the `.cpp` file plus the matching header.

| Parser | Lines | Commits since 2023-09-24 |
|---|---|---|
| `BRDFile` | 178 | 0 |
| `BRD2File` | 242 | 0 |
| `BDVFile` | 163 | 0 |
| `ASCFile` | 218 | 0 |
| `BVRFile` | 175 | 0 |
| `BVR3File` | 251 | 1 |
| `CADFile` | 184 | 0 |
| `CSTFile` | 182 | 0 |
| `BRDFileBase` | 240 | 0 |
| `ADFile` | 547 | 0 |
| `FZFile` | 579 | 5 |
| `CAEFile` | 29 | 2 (new format, 2025-01-08, issue 162) |
| `XZZPCBFile` | 568 | 2 (new format, 2025-08-15, pull 334) |
| `GenCADFile` plus `GenCADFileBnf.h` | 1035 | 9 |
| `BRDAllegroFile` | 17 | 0 (reject only) |

The stable parsers are small. They did not change in three years. A rewrite of those parsers is not necessary. A copy of the MIT code is enough.

GenCAD is the expensive parser. Nine commits fix bad exports: extra header fields, missing sections, duplicate parts, and whitespace. The file `GenCADFile.cpp` comments name poor files (for example LA-K261P, commit `00745255f`, 2024-03-20).

FZ quirks, from `FZFile.cpp`:

- The key is 44 values of type `uint32_t`. The parser checks the key. If the user key fails, it tries a built-in key (commit `0a52009ce`, 2024-07-24). CAE uses a different built-in key (commit `31f450b0d`, 2025-08-19).
- Decode is RC6, then a split, then zlib. Some files are zlib only and start with bytes `78 9C` or `78 DA`.
- Some files use a comma as the decimal mark. The parser replaces commas with dots.
- Two pin-column layouts exist. One uses `PIN_NUMBER`. The other uses `PIN_NAME` because `PIN_NUMBER` is always `0` (commit `165e4809f`, 2024-07-28).
- `srotate` is discarded. The outline is a rectangle around the pins.
- Side placement was wrong and was fixed on 2026-03-07 (commit `2241772ec`).

Do not copy key bytes into this document or into git history comments.

XZZ is DES-encrypted and still logs unknown blocks. Allegro binary is not supported. Those two formats are the high maintenance risk. Leave them out of the first JSON program.

## Risks

- A file can use a new vendor quirk. GenCAD and FZ are the likely files.
- FZ and CAE fail closed when the key check fails. The JSON program must return that error. It must not invent coordinates.
- Internal coordinates are mils. A later photo map must keep that unit.
- Part centers from pins are not the courtyard center.
- `setlocale` in the parsers is safe only in a subprocess.
- GPL and AGPL parsers (`pcbrepair`, BoardRipper) must stay out of the MCP process.

## MCP tools

The server loads one board into memory. Coordinates in the tool results are mils. The side is `top`, `bottom`, or `both`.

1. `board_open(path)` opens one file. It returns the format name, the counts, and the error text when the parse fails.
2. `board_list_parts(query)` returns part names, side, and center.
3. `board_part(name)` returns one part, its pins, each pin net, and each pin x/y.
4. `board_net(name)` returns the pins on that net, with part name, pin name, side, and x/y.
5. `board_outline()` returns the outline points in order.
6. `board_set_photo_points(part_name, pixel_x, pixel_y)` stores 4 reference pairs for the open photo.
7. `board_to_pixel(name)` returns pixel x/y for a part after the 4 points exist.

Tool 6 and tool 7 are the homography step. They do not move the phone. The existing camera tools do that.
