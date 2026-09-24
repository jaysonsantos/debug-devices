# Bench instructions

Copy this file to `instructions.md` at the repo root and fill it in. Git ignores `instructions.md`.
When the debug-devices MCP server starts, the agent reads this file first (tool `bench_instructions`).
The file is read again on each call, so edits apply without a restart.
Do not put secrets here.

## Device under test

- Device: <for example: laptop mainboard, model and revision>
- Fault: <what the user sees, for example: no power, no display, fan spins then stops>
- Known history: <repairs, parts replaced, liquid damage>

## Board file

- Path: <absolute path of the boardview file, for example /home/me/boards/board.cad>
- Open it with `board_open` at the start of a session.

## Bench set-up

- PC webcam: points at <the multimeter LCD>.
- Phone camera: points at <the board, top side or bottom side>.
- Multimeter: <make and model>. Probes: <where they are, for example black on GND of the charger port>.
- Power supply: <bench supply or charger, voltage, current limit>.

## Safety limits

- Maximum voltage: <for example 20 V>. Maximum current: <for example 3 A>.
- Never short: <for example the battery connector, the CPU core rail to GND>.
- Before a measurement in a resistance or diode mode, the board must have no power. Ask the user to remove it.
- Stop and ask the user before any step that can damage the board.

## Usual workflow

1. Start the bench (`bench_start`, with `board_path` from the section "Board file").
2. Check the main rails first: <rail names, for example PP19V, PP3V3_S5, PP5V_S5>.
3. For each rail: find the net (`board_find_net`), the nearest test point, then measure (`multimeter_read`).
4. Record each result: rail, expected value, measured value.

## Preferences

- Language: <for example English, short sentences>.
- Detail: <for example: short answers; show the board image only when I ask>.
- Before you tell me to probe a point, show me where it is (`board_render` with `crop_to_part`).
