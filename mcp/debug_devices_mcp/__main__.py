"""Entry point: `debug-devices-mcp` runs the MCP server on stdio."""

import logging

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import QUIET_LOGGERS
from debug_devices_mcp.server import Services, build_server


def main() -> None:
    settings = Settings.from_cli()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    build_server(Services.from_settings(settings)).run("stdio")


if __name__ == "__main__":
    main()
