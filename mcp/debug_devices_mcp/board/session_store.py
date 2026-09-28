"""Keep the board session over a server restart: the open board (path and hash) and the photo registrations.

The record is a small JSON file in the user's state directory, never in the repository. After a restart, the first
board tool opens the board again (obv-dump). The restored registrations are stale: a new server process cannot check
their scene (its capture ids and scene reference are gone), so the agent registers a fresh photo.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

SESSION_FILE_NAME = "board-session.json"
RECORD_VERSION = 1


class BoardSessionRecord(BaseModel):
    version: int = RECORD_VERSION
    saved_at: AwareDatetime | None = None
    board_path: str | None = None
    board_sha256: str | None = None
    # Registration models as JSON (board/tools.py owns the model; this module does not import it).
    registrations: list[dict[str, object]] = Field(default_factory=list)
    last_registration_id: str | None = None
    # board_open side_labels: "auto", "mixed", or "trust".
    side_labels: str = "auto"


class BoardSessionStore:
    """Without a path: memory only (tests and servers built without settings)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._memory = BoardSessionRecord()

    def load(self) -> BoardSessionRecord:
        if self.path is None:
            return self._memory.model_copy(deep=True)
        try:
            return BoardSessionRecord.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return BoardSessionRecord()
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            logger.warning("cannot read the board session %s, starting without it: %s", self.path, exc)
            return BoardSessionRecord()

    def save(self, record: BoardSessionRecord) -> None:
        record.saved_at = datetime.now(UTC)
        if self.path is None:
            self._memory = record.model_copy(deep=True)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(record.model_dump_json(indent=2) + "\n")
            temporary.replace(self.path)
        except OSError as exc:
            logger.warning("cannot save the board session %s: %s", self.path, exc)
