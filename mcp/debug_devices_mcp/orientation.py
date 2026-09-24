"""The phone snapshot orientation (flips): one state for the tools, the monitor page, and the settings file."""

import logging
from collections.abc import Callable

from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.ui.settings import SettingsStore

logger = logging.getLogger(__name__)

type OrientationListener = Callable[[SnapshotOrientation], None]


class OrientationState:
    """The current flips. It reads the settings file at start and saves each change there (also without the UI)."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        saved = store.load().snapshot_orientation if store is not None else None
        self.current = saved or SnapshotOrientation()
        self._listeners: list[OrientationListener] = []

    def add_listener(self, listener: OrientationListener) -> None:
        self._listeners.append(listener)

    def update(self, flip_horizontal: bool | None = None, flip_vertical: bool | None = None) -> SnapshotOrientation:
        """Change the given flips (None keeps a flip), save, and tell the listeners."""
        self.current = SnapshotOrientation(
            flip_horizontal=self.current.flip_horizontal if flip_horizontal is None else flip_horizontal,
            flip_vertical=self.current.flip_vertical if flip_vertical is None else flip_vertical,
        )
        if self._store is not None:
            try:
                saved = self._store.load()
                self._store.save(saved.model_copy(update={"snapshot_orientation": self.current}))
            except OSError as exc:
                logger.warning("cannot save the snapshot orientation: %s", exc)
        for listener in self._listeners:
            listener(self.current)
        return self.current
