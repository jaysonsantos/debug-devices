"""Local only: load the real board in BOARDVIEW_TARGET with the real obv-dump.

The target file is proprietary. This test reads it only on this PC, and it never copies any part of it.
It skips when BOARDVIEW_TARGET is not set or obv-dump is not on PATH (or in BOARDVIEW_DUMP_BIN).
Run: BOARDVIEW_TARGET=/path/to/board.cad uv run pytest mcp/tests/test_board_target.py -s
"""

import os
import shutil
from pathlib import Path

import pytest

from debug_devices_mcp.board.constants import defaults, env
from debug_devices_mcp.board.loader import BoardviewLoader, LoaderOptions
from debug_devices_mcp.process import SubprocessRunner

# The whole load (hash, obv-dump, JSON, index) must fit in this time.
MAX_LOAD_SECONDS = 10.0
MIN_PARTS = 1000

TARGET = os.environ.get(env.TARGET, "")
DUMP_BIN = shutil.which(os.environ.get(env.DUMP_BIN, defaults.DUMP_BIN))

pytestmark = pytest.mark.skipif(
    not TARGET or not Path(TARGET).is_file() or DUMP_BIN is None,
    reason=f"local only: set {env.TARGET} to a board file and put obv-dump on PATH",
)


async def test_target_loads_fast_and_has_parts() -> None:
    assert DUMP_BIN is not None
    loader = BoardviewLoader(SubprocessRunner(), LoaderOptions(dump_bin=DUMP_BIN, timeout=defaults.DUMP_TIMEOUT))

    board, info = await loader.load(Path(TARGET))
    _, cached = await loader.load(Path(TARGET))

    print(f"parts={len(board.parts)} nets={len(board.pins_by_net)} load={info.load_seconds:.2f}s")
    assert info.load_seconds < MAX_LOAD_SECONDS
    assert cached.cached
    assert len(board.parts) > MIN_PARTS
    assert board.pins_by_net
    # A few kinds of parts that every laptop mainboard has.
    for pattern in ("C*", "R*", "U*"):
        assert board.find_parts(pattern), pattern
