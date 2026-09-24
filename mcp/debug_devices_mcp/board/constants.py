"""Names, defaults, and limits of the boardview tools."""

from datetime import timedelta


class env:
    """Environment variable names from .env.example. They have no DEBUG_DEVICES_ prefix."""

    DUMP_BIN = "BOARDVIEW_DUMP_BIN"
    FZ_KEY = "BOARDVIEW_FZ_KEY"
    CAE_KEY = "BOARDVIEW_CAE_KEY"
    XZZ_KEY = "BOARDVIEW_XZZ_KEY"
    TARGET = "BOARDVIEW_TARGET"


class defaults:
    DUMP_BIN = "obv-dump"
    DUMP_TIMEOUT = timedelta(seconds=60)
    NEAR_RADIUS_MM = 5.0
    FIND_LIMIT = 50
    RENDER_MAX_SIDE = 1568


class dump:
    """`obv-dump` command line, from docs/boardview-json.md."""

    SCHEMA_VERSION = 1
    FZ_KEY_FLAG = "--fz-key"
    CAE_KEY_FLAG = "--cae-key"
    XZZ_KEY_FLAG = "--xzz-key"
    EXIT_OK = 0
    EXIT_PARSE_FAILED = 1


class units:
    MIL_PER_INCH = 1000
    MM_PER_INCH = 25.4


# A box made from pins gets this margin (or the pin radius when it is larger), so a 2-pin part is not a line.
PIN_BOX_MARGIN_MM = 0.3
# Part name prefixes that mark a test point part (besides the nails of the format).
TEST_POINT_PREFIXES = ("TP", "PT", "TEST")
HASH_CHUNK_BYTES = 1024 * 1024
