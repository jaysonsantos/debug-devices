"""Read an SMD value code (for example "100", "4R7", "01C") as a resistance. The result is an interpretation only.

A short marking on a small part is often its value, not its name. The same text can also be a part name, a date
code, or a code of another maker, so every result says which code it assumes.
"""

import re
from enum import StrEnum

from pydantic import BaseModel

OHM = "Ω"
DECIMAL_DIGITS = 10
# EIA 3-digit and 4-digit codes: the last digit is the power of ten. 8 and 9 are not multipliers for resistors.
MAX_POWER_DIGIT = 7
EIA_3_DIGIT_LENGTH = 3
# EIA-96 (1% resistors): a two-digit index into the E96 series, then a multiplier letter.
E96_VALUES = (
    100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133, 137, 140, 143, 147, 150, 154, 158, 162, 165,
    169, 174, 178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232, 237, 243, 249, 255, 261, 267, 274, 280,
    287, 294, 301, 309, 316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412, 422, 432, 442, 453, 464, 475,
    487, 499, 511, 523, 536, 549, 562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732, 750, 768, 787, 806,
    825, 845, 866, 887, 909, 931, 953, 976,
)  # fmt: skip
E96_MULTIPLIERS = {
    "Z": 0.001,
    "Y": 0.01,
    "R": 0.01,
    "X": 0.1,
    "S": 0.1,
    "A": 1.0,
    "B": 10.0,
    "H": 10.0,
    "C": 100.0,
    "D": 1_000.0,
    "E": 10_000.0,
    "F": 100_000.0,
}
# A letter that stands for the decimal point, with its factor ("4R7" = 4.7, "4K7" = 4 700, "1M0" = 1 000 000).
DECIMAL_LETTERS = {"R": 1.0, "K": 1_000.0, "M": 1_000_000.0}
PREFIXES = ((1_000_000.0, "M"), (1_000.0, "k"), (1.0, ""))
SIGNIFICANT_DIGITS = 3

EIA_DIGITS = re.compile(r"\d{3,4}")
# A zero-ohm jumper is often marked with a single 0 (or 00).
JUMPER = re.compile(r"0{1,2}")
EIA_96 = re.compile(r"(\d{2})([A-Z])")
LETTER_DECIMAL = re.compile(r"(\d*)([RKM])(\d*)")


class ValueCodeKind(StrEnum):
    JUMPER = "jumper"
    EIA_3_DIGIT = "eia_3_digit"
    EIA_4_DIGIT = "eia_4_digit"
    EIA_96 = "eia_96"
    LETTER_DECIMAL = "letter_decimal"


class ValueInterpretation(BaseModel):
    # The code as read (upper case, no spaces).
    code: str
    kind: ValueCodeKind
    value_ohm: float
    display: str
    # What the code assumes: a value code is never proof of the part.
    note: str
    interpretation_only: bool = True


NOTES = {
    ValueCodeKind.JUMPER: "A 0 on a resistor-size part is a zero-ohm jumper. Interpretation only.",
    ValueCodeKind.EIA_3_DIGIT: "EIA 3-digit resistor code: two digits, then the power of ten. Interpretation only.",
    ValueCodeKind.EIA_4_DIGIT: "EIA 4-digit resistor code (1%): three digits, then the power of ten. "
    "Interpretation only.",
    ValueCodeKind.EIA_96: "EIA-96 resistor code (1%): index into the E96 series, then a multiplier letter. "
    "Interpretation only.",
    ValueCodeKind.LETTER_DECIMAL: "The letter is the decimal point (R = ohm, K = kilo-ohm, M = mega-ohm). "
    "On an inductor, R codes are microhenry (4R7 = 4.7 µH). Interpretation only.",
}


def display(value_ohm: float) -> str:
    """Engineering notation with three significant digits: 10 Ω, 4.7 kΩ, 0.19 Ω."""
    for factor, prefix in PREFIXES:
        if value_ohm >= factor:
            return f"{value_ohm / factor:.{SIGNIFICANT_DIGITS}g} {prefix}{OHM}"
    return f"{value_ohm:.{SIGNIFICANT_DIGITS}g} {OHM}"


def interpretation(code: str, kind: ValueCodeKind, value_ohm: float) -> ValueInterpretation:
    note = NOTES[kind]
    if value_ohm == 0 and kind is not ValueCodeKind.JUMPER:
        note = f"{note} 0 Ω is a jumper."
    return ValueInterpretation(code=code, kind=kind, value_ohm=value_ohm, display=display(value_ohm), note=note)


def decode_value_marking(text: str) -> list[ValueInterpretation]:
    """Every value-code reading of `text`. Empty when it is not a known code. More than one when it is ambiguous."""
    code = re.sub(r"\s+", "", text).upper()
    results: list[ValueInterpretation] = []
    if JUMPER.fullmatch(code):
        results.append(interpretation(code, ValueCodeKind.JUMPER, 0.0))
    if EIA_DIGITS.fullmatch(code) and int(code[-1]) <= MAX_POWER_DIGIT:
        kind = ValueCodeKind.EIA_3_DIGIT if len(code) == EIA_3_DIGIT_LENGTH else ValueCodeKind.EIA_4_DIGIT
        results.append(interpretation(code, kind, int(code[:-1]) * DECIMAL_DIGITS ** int(code[-1])))
    if (match := EIA_96.fullmatch(code)) and match.group(2) in E96_MULTIPLIERS:
        index = int(match.group(1))
        if 1 <= index <= len(E96_VALUES):
            value = E96_VALUES[index - 1] * E96_MULTIPLIERS[match.group(2)]
            results.append(interpretation(code, ValueCodeKind.EIA_96, round(value, SIGNIFICANT_DIGITS + 3)))
    if (match := LETTER_DECIMAL.fullmatch(code)) and (match.group(1) or match.group(3)):
        whole, letter, fraction = match.groups()
        value = float(f"{whole or '0'}.{fraction or '0'}") * DECIMAL_LETTERS[letter]
        results.append(interpretation(code, ValueCodeKind.LETTER_DECIMAL, value))
    # Two codes can give the same value ("01R" is 1 Ω as EIA-96 and as R notation): keep the first.
    unique: dict[float, ValueInterpretation] = {}
    for item in results:
        unique.setdefault(item.value_ohm, item)
    return list(unique.values())
