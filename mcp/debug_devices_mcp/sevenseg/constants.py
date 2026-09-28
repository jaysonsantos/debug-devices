"""Names, defaults, and limits of the local 7-segment decoder."""

from datetime import timedelta
from enum import StrEnum

PROGRAM_NAME = "debug-devices-sevenseg"
# The folder of the profile and the dataset, inside the state folder of debug-devices (ui/settings.py state_dir).
STATE_SUBDIR = "sevenseg"
PROFILE_FILE_NAME = "profile.json"
DATASET_DIR_NAME = "dataset"
ANNOTATED_SUFFIX = ".annotated.png"
JPEG_SUFFIX = ".jpg"
JSON_SUFFIX = ".json"
TEMPLATE_T21D = "proster-t21d"


class LocalDecoderMode(StrEnum):
    """`--meter-local-decoder`: off runs no local code; compare adds the local reading next to the vision result."""

    OFF = "off"
    COMPARE = "compare"


class warp:
    # The warped LCD is this many pixels high; the width follows the aspect ratio of the calibrated corners.
    HEIGHT = 200
    MIN_WIDTH = 100
    MAX_ASPECT = 6.0
    MIN_ASPECT = 1.0


class score:
    # The background estimate removes dark strokes narrower than this part of the LCD height (a morphological close).
    BACKGROUND_KERNEL_FRACTION = 0.16
    BACKGROUND_BLUR_FRACTION = 0.08
    # The "ink" level of a frame: this percentile of the darkness inside the digit boxes.
    INK_PERCENTILE = 97.0
    # Below this ink level (darkness 0..1) the LCD is too faint to read: the frame is unreadable.
    MIN_CONTRAST = 0.12
    # A pixel is ink when its darkness is above this part of the ink level (the adaptive threshold of each frame).
    INK_THRESHOLD_FRACTION = 0.5
    # A region is on when this part of its pixels is ink.
    FILL_ON = 0.5
    # A fill this far from FILL_ON gives the full confidence 1.0; nearer is less sure.
    FILL_MARGIN = 0.3
    # A symbol region holds a thin glyph ("AUTO", "kΩ"): the glyph covers only a part of its box.
    SYMBOL_FILL_ON = 0.1
    SYMBOL_FILL_MARGIN = 0.06
    # The ink threshold of a symbol pixel, as a part of the frame threshold.
    SYMBOL_THRESHOLD_FRACTION = 0.6
    # A read with an overall confidence below this is "uncertain".
    MIN_CONFIDENCE = 0.4
    # A digit that matches a known pattern only with this many segments changed.
    NEAREST_MAX_DISTANCE = 1
    NEAREST_CONFIDENCE_FACTOR = 0.5


class stability:
    # Standalone sampling: frames per second, the window, and the frames that must agree.
    RATE_HZ = 5.0
    WINDOW = timedelta(seconds=1)
    MIN_AGREE = 3


class calibration:
    # The LCD is the largest bright quadrilateral: its area is at least this part of the crop.
    MIN_LCD_AREA_FRACTION = 0.05
    # approxPolyDP epsilon as a part of the contour perimeter.
    POLY_EPSILON_FRACTION = 0.02
    QUAD_CORNERS = 4
    # The fit moves and scales the digit block of the template to make the segments most decisive.
    FIT_SHIFTS = (-0.04, -0.02, 0.0, 0.02, 0.04)
    FIT_SCALES = (0.94, 1.0, 1.06)
    FINE_SHIFTS = (-0.01, -0.005, 0.0, 0.005, 0.01)
    FINE_SCALES = (0.98, 1.0, 1.02)
    CLOSE_KERNEL_FRACTION = 0.02
    ANNOTATION_SCALE = 3
    ON_COLOR = (0, 200, 0)
    OFF_COLOR = (0, 0, 220)
    LCD_COLOR = (255, 160, 0)
    LINE_THICKNESS = 1


class dataset:
    # The compare-mode dataset keeps at most this many frames; the oldest go first.
    MAX_ENTRIES = 500
