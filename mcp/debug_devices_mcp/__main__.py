"""Entry point: `debug-devices-mcp` runs the MCP server on stdio."""

import logging

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import QUIET_LOGGERS
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.shutdown import arm_exit_watchdog
from debug_devices_mcp.ui.setup import build_monitor


def main() -> None:
    settings = Settings.from_cli()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    services = Services.from_settings(settings)
    monitor = build_monitor(settings, services) if settings.ui else None
    # In a terminal, the stdin thread of the SDK keeps the process alive after Ctrl-C. The watchdog ends it.
    build_server(services, monitor, after_stop=arm_exit_watchdog).run("stdio")


if __name__ == "__main__":
    main()
