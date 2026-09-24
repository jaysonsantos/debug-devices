"""The one pair of unit helpers: `obv-dump` gives mil, the tools use mm."""

from debug_devices_mcp.board.constants import units

MM_PER_MIL = units.MM_PER_INCH / units.MIL_PER_INCH


def mil_to_mm(value: float) -> float:
    return value * MM_PER_MIL


def mm_to_mil(value: float) -> float:
    return value / MM_PER_MIL
