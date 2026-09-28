"""The point name of a residual-voltage reading (bench_state.py): one physical point, a part pin or a net.

The safety gate keeps the latest residual reading of each point, so one name must not stand for two points. With a
board open, the name must be a part pin ("C12.1", "C12 pin 1") or a net of that board. Without a board, a free name
is accepted with a warning. A generic name ("residual", "test", "point", empty) and a ground net (GND, AGND, VSS, and
the other GROUND_NET_PATTERNS, also a pin on such a net) are always refused.
"""

import fnmatch
import re

from pydantic import BaseModel

from debug_devices_mcp.board.model import Board, is_glob

# Words that do not name a point. A name made only of these words and numbers is generic.
GENERIC_WORDS = frozenset(
    {"residual", "voltage", "volts", "volt", "test", "point", "measurement", "reading", "here", "probe", "check", "dc"}
)
# "C12.1", "C12:1", "C12 pin 1", "PC12 pin 1".
PART_PIN = re.compile(r"^(?P<part>[^\s.:]+)\s*(?:[.:]|\s+pin\s+)\s*(?P<pin>[^\s.:]+)$", re.IGNORECASE)
MAX_LISTED_PINS = 6
NAME_THE_POINT = "name the point: part.pin or net (for example C12.1 or PP3V3_S5)"
# Ground nets: a reading there is always about 0 V, so it is not a residual point (and never "the safe reading").
GROUND_NET_PATTERNS = ("*GND*", "*GRND*", "*GROUND*", "*VSS*", "0V", "EARTH*", "CHASSIS*")


class PointName(BaseModel):
    """A checked point name."""

    # The key of the point: "c12.1" for every spelling of that pin. Two readings with one key are one point.
    key: str
    # For the answer: why the name is accepted with a warning (no board open), or None.
    warning: str | None = None


class PointNameError(ValueError):
    """The name does not name one point."""


def words(label: str) -> list[str]:
    return label.casefold().replace("_", " ").replace("-", " ").split()


def is_ground(name: str) -> bool:
    # Without spaces: "0 V" is "0V".
    text = name.casefold().replace(" ", "")
    return any(fnmatch.fnmatchcase(text, pattern.casefold()) for pattern in GROUND_NET_PATTERNS)


def ground_error(label: str, net: str) -> PointNameError:
    return PointNameError(
        f"{NAME_THE_POINT}: {label!r} is on the ground net {net}; a reading there is always about 0 V, so it is not a "
        "residual point"
    )


def is_generic(label: str) -> bool:
    return all(word in GENERIC_WORDS or word.isdigit() for word in words(label))


def text_key(label: str) -> str:
    """The key without a board: the part.pin form when the name has it, else the name in one case and spacing."""
    text = " ".join(label.split())
    match = PART_PIN.match(text)
    return f"{match['part']}.{match['pin']}".casefold() if match else text.casefold()


def point_name(label: str, board: Board | None) -> PointName:
    """Check a residual point name. Raise PointNameError when it does not name one point."""
    text = " ".join(label.split())
    if is_generic(text):
        raise PointNameError(f"{NAME_THE_POINT}, not {label!r}")
    if is_ground(text):
        raise ground_error(label, text)
    if board is None:
        return PointName(
            key=text_key(text),
            warning=(
                f"no board is open, so {text!r} is not checked: use this name only for this one point, and the same "
                "name when you measure it again"
            ),
        )
    if is_glob(text):
        raise PointNameError(f"{NAME_THE_POINT}: {label!r} is a pattern, not one point")
    nets = board.net_names(text)
    if nets:
        if is_ground(nets[0]):
            raise ground_error(label, nets[0])
        return PointName(key=nets[0].casefold())
    match = PART_PIN.match(text)
    part = board.part(match["part"] if match else text)
    if part is None:
        raise PointNameError(f"{NAME_THE_POINT}: {label!r} is not a part pin or a net of the open board")
    pins = board.pins_by_part.get(part.name, [])
    if match is None:
        numbers = ", ".join(f"{part.name}.{pin.number}" for pin in pins[:MAX_LISTED_PINS])
        raise PointNameError(f"{NAME_THE_POINT}: {label!r} is a part; name its pin ({numbers})")
    wanted = match["pin"].casefold()
    pin = next((pin for pin in pins if wanted in {pin.number.casefold(), pin.name.casefold()}), None)
    if pin is None:
        raise PointNameError(f"{NAME_THE_POINT}: {part.name} has no pin {match['pin']!r}")
    if is_ground(pin.net):
        raise ground_error(label, pin.net)
    return PointName(key=f"{part.name}.{pin.number}".casefold())
