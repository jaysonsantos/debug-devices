"""The LCD symbols that are on give the unit, the mode, and the flags."""

from debug_devices_mcp.meter_mode import MeterMode
from debug_devices_mcp.sevenseg.profile import Symbol

UNKNOWN_UNIT = "unknown"
PREFIXES: dict[Symbol, str] = {
    Symbol.NANO: "n",
    Symbol.MICRO: "µ",  # MICRO SIGN, as the vision model writes it
    Symbol.MILLI: "m",
    Symbol.KILO: "k",
    Symbol.MEGA: "M",
}
BASES: dict[Symbol, str] = {
    Symbol.VOLT: "V",
    Symbol.AMPERE: "A",
    Symbol.OHM: "Ω",  # Greek capital omega
    Symbol.FARAD: "F",
    Symbol.HERTZ: "Hz",
    Symbol.PERCENT: "%",
    Symbol.CELSIUS: "°C",
    Symbol.FAHRENHEIT: "°F",
}
FLAGS: dict[Symbol, str] = {
    Symbol.AUTO: "AUTO",
    Symbol.HOLD: "HOLD",
    Symbol.LOW_BATTERY: "low battery",
    Symbol.REL: "REL",
}
# The symbols that decide the unit or the mode: their confidence counts for the reading.
DECIDING = frozenset(PREFIXES) | frozenset(BASES) | {Symbol.AC, Symbol.DC, Symbol.DIODE, Symbol.CONTINUITY}


def unit_of(on: set[Symbol]) -> tuple[str, list[str]]:
    """The unit text ("mV", "kΩ") and the problems. One base symbol and at most one prefix make a unit."""
    bases = [text for symbol, text in BASES.items() if symbol in on]
    prefixes = [text for symbol, text in PREFIXES.items() if symbol in on]
    problems = []
    if not bases:
        problems.append("no unit symbol is on")
    elif len(bases) > 1:
        problems.append(f"several unit symbols are on ({', '.join(bases)})")
    if len(prefixes) > 1:
        problems.append(f"several unit prefixes are on ({', '.join(prefixes)})")
    if problems:
        return UNKNOWN_UNIT, problems
    return (prefixes[0] if prefixes else "") + bases[0], []


def mode_of(on: set[Symbol]) -> MeterMode:
    """The mode from the symbols, with the rules of the vision prompt. Without an AC symbol, V and A are DC."""
    if Symbol.CONTINUITY in on:
        return MeterMode.CONTINUITY
    if Symbol.DIODE in on:
        return MeterMode.DIODE
    alternating = Symbol.AC in on and Symbol.DC not in on
    rules: tuple[tuple[Symbol, MeterMode], ...] = (
        (Symbol.VOLT, MeterMode.AC_VOLTAGE if alternating else MeterMode.DC_VOLTAGE),
        (Symbol.AMPERE, MeterMode.AC_CURRENT if alternating else MeterMode.DC_CURRENT),
        (Symbol.OHM, MeterMode.RESISTANCE),
        (Symbol.FARAD, MeterMode.CAPACITANCE),
        (Symbol.HERTZ, MeterMode.FREQUENCY),
        (Symbol.PERCENT, MeterMode.DUTY_CYCLE),
        (Symbol.CELSIUS, MeterMode.TEMPERATURE),
        (Symbol.FAHRENHEIT, MeterMode.TEMPERATURE),
    )
    return next((mode for symbol, mode in rules if symbol in on), MeterMode.OTHER)


def flags_of(on: set[Symbol]) -> list[str]:
    return [text for symbol, text in FLAGS.items() if symbol in on]
