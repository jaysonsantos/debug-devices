"""Which requests reach the page: the Host and Origin checks.

The page listens on 127.0.0.1 only and accepts the host names 127.0.0.1 and localhost. `--ui-allowed-origin` adds
exact origins (for example `https://bench.example.org`, an https tunnel to the page). It is off by default. A tunnel
must have its own login: the page has none, and it controls the phone and the camera.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from debug_devices_mcp.ui.constants import http

ORIGIN_SCHEMES = {"http": 80, "https": 443}
NOT_AN_ORIGIN = "{value!r} is not an origin: use scheme://host[:port], for example https://bench.example.org"


def normalize_origin(value: str) -> str:
    """The form that a browser sends in the Origin header: a lower-case scheme and host, no default port, no path."""
    parts = urlsplit(value.strip())
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(NOT_AN_ORIGIN.format(value=value)) from exc
    extra = parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username or parts.password
    if parts.scheme not in ORIGIN_SCHEMES or not parts.hostname or extra:
        raise ValueError(NOT_AN_ORIGIN.format(value=value))
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return (
        f"{parts.scheme}://{host}"
        if port in {None, ORIGIN_SCHEMES[parts.scheme]}
        else f"{parts.scheme}://{host}:{port}"
    )


def host_name(host_header: str) -> str | None:
    return urlsplit(f"//{host_header}").hostname


@dataclass(frozen=True)
class PageOrigins:
    """The local host names (any port, as before) and the exact origins of `--ui-allowed-origin`."""

    allowed: frozenset[str] = frozenset()

    @classmethod
    def of(cls, origins: tuple[str, ...] | list[str]) -> PageOrigins:
        return cls(frozenset(normalize_origin(origin) for origin in origins))

    def is_allowed(self, origin: str) -> bool:
        """An exact allowed origin. A header that is not an origin is not allowed."""
        try:
            return normalize_origin(origin) in self.allowed
        except ValueError:
            return False

    def host_allowed(self, host_header: str | None) -> bool:
        """The Host header names this page: a local name, or the host of an allowed origin (a tunnel that keeps the
        Host of the browser). Anything else can be DNS rebinding."""
        if not host_header:
            return False
        host = host_name(host_header)
        return host in http.ALLOWED_HOSTS or host in {urlsplit(origin).hostname for origin in self.allowed}

    def origin_allowed(self, origin: str) -> bool:
        """A write from this origin can be the page: a local host name, or an exact allowed origin."""
        return urlsplit(origin).hostname in http.ALLOWED_HOSTS or self.is_allowed(origin)

    def from_the_page(self, origin: str | None, host_header: str | None) -> bool:
        """A same-origin request (the Origin names this Host), or a request from an allowed origin: a tunnel can
        change the Host header (for example to 127.0.0.1), so the origin alone decides there."""
        if origin is None:
            return False
        return urlsplit(origin).netloc == host_header or self.is_allowed(origin)
