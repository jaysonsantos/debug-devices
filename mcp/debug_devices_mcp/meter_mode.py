"""The measurement modes of the multimeter. multimeter.py and the local decoder (sevenseg) share them."""

from enum import StrEnum


class MeterMode(StrEnum):
    DC_VOLTAGE = "dc_voltage"
    AC_VOLTAGE = "ac_voltage"
    DC_CURRENT = "dc_current"
    AC_CURRENT = "ac_current"
    RESISTANCE = "resistance"
    CONTINUITY = "continuity"
    DIODE = "diode"
    CAPACITANCE = "capacitance"
    FREQUENCY = "frequency"
    TEMPERATURE = "temperature"
    DUTY_CYCLE = "duty_cycle"
    OTHER = "other"
