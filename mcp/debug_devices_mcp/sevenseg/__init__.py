"""A local decoder for the 7-segment LCD of the multimeter (compare mode only).

It reads the webcam crop of the meter in milliseconds: a perspective warp of the LCD to a fixed size, then a dark or
light score for each calibrated region (segments, decimal points, sign, symbols). Pure functions, no network. The
vision model result stays the measurement: the local result is only compared with it.

This package file stays empty on purpose: `multimeter.py` can import `sevenseg.reading` without the decoder.
"""
