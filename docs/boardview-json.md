# Boardview JSON (contract)

`obv-dump` (C++, `boardview/`) and the MCP server (`mcp/`) share this contract.
Change this file first, then change both sides.

## Command

```sh
obv-dump <file> [--fz-key <hex>] [--cae-key <hex>] [--xzz-key <hex>]
```

- Output: one JSON document on stdout, UTF-8.
- Exit 0: the parse succeeded. Exit 1: the parse failed; stdout still has a JSON `DumpError`. Exit 2: bad command line; usage text on stderr, no JSON.
- Stderr: log text only. The MCP server does not parse it.
- The MCP server runs `obv-dump` as a subprocess with a timeout. A crash (signal) or a timeout is a parse failure.
- Keys come only from the command line. `obv-dump` never ships a key.
- `--fz-key` and `--cae-key`: 44 hex words (`0x` prefix optional), separated by commas or spaces, as in `obv.conf`. `--xzz-key`: one 64-bit hex value.

## `BoardDump` (exit 0)

```json
{
  "schema_version": 1,
  "source": {"path": "/abs/path/file.cad", "format": "gencad", "obv_version": "10.0.0"},
  "units": "mil",
  "outline": [{"x": 0, "y": 0}],
  "outline_segments": [{"a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}}],
  "parts": [
    {
      "name": "U2",
      "side": "top",
      "mounting": "smd",
      "p1": {"x": 100, "y": 200},
      "p2": {"x": 300, "y": 400},
      "rotation_deg": 90.0,
      "mfgcode": "",
      "first_pin": 0,
      "pin_count": 8
    }
  ],
  "pins": [
    {
      "part": "U2",
      "number": "1",
      "name": "VCC",
      "net": "PP3V3",
      "x": 120,
      "y": 210,
      "side": "top",
      "radius": 10.0,
      "probe": 0
    }
  ],
  "nails": [{"net": "PP3V3", "x": 500, "y": 500, "side": "bottom", "probe": 1}]
}
```

Rules:

- Coordinates are integers in mil (1/1000 inch), the OpenBoardView `BRDPoint` unit. The MCP server converts to mm at its boundary.
- `format`: one of `brd`, `brd2`, `bdv`, `asc`, `bvr`, `bvr3`, `cad`, `cst`, `fz`, `cae`, `gencad`, `ad`, `xzz`.
- `side`: `top`, `bottom`, or `both`.
- `mounting`: `smd` or `through_hole`.
- `p1` and `p2`: the part box when the format has it. `null` when the parser leaves them at 0,0. When `p1 == p2`, the point is the placement point (GenCAD), not a box. In both cases the MCP server computes the box from the pins.
- `rotation_deg`: the value from the file in degrees, with no change for the bottom side. Set for `gencad` (0 when the component has no `ROTATION`), `ad`, and `fz` (only when the field is a number). `null` for the other formats. `0.0` can also mean that a converter put the rotation into the shape.
- `first_pin`: `null` when `pin_count` is 0.
- Test pads: OpenBoardView adds dummy parts named `...` for test pads. `obv-dump` does not write them. It writes their pins as `nails`, without duplicates (same x, y, side, and net).
- Repeated part names get a suffix: the second `REF**` is `REF**#2`, then `REF**#3`. A suffix never collides with a name in the file.
- Pin `number`: when the format has no pin numbers (`brd2`), the MCP server numbers the pins in file order from 1, as OpenBoardView does.
- `outline` and `outline_segments` can both be empty.
- `pins[].part` is the part `name`. Part names are unique in one dump.
- Empty text fields are `""`, not `null`.

## `DumpError` (exit 1)

```json
{"schema_version": 1, "error": "parse_failed", "message": "the $SHAPES section was not parsed properly", "format": "gencad"}
```

Error codes: `unknown_format`, `parse_failed`, `key_required`, `key_invalid`, `io_error`.

`format` is `null` when the format is not known. An Allegro binary `.brd` gives `unknown_format`, `format: null`, and the message "Allegro binary .brd files are not supported".
