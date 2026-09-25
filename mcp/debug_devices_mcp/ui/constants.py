"""Names, defaults, and limits of the monitor window, the webcam stream, and scrcpy."""

from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

STATE_DIR_NAME = "debug-devices"
# `/api/whoami` answers with this name. Other MCP processes check it before they read frames from the port.
APP_NAME = "debug-devices-monitor"
SETTINGS_FILE_NAME = "ui-settings.json"
SCRCPY_LOG_FILE_NAME = "scrcpy.log"
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = "index.html"


class UiStart(StrEnum):
    """When the monitor starts its page and the webcam stream."""

    # At the first tool call (page) and the first webcam use (stream). Nothing at process start.
    LAZY = "lazy"
    # At process start, like a dev monitor.
    EAGER = "eager"


class env:
    XDG_STATE_HOME = "XDG_STATE_HOME"
    XDG_RUNTIME_DIR = "XDG_RUNTIME_DIR"
    WAYLAND_DISPLAY = "WAYLAND_DISPLAY"
    DISPLAY = "DISPLAY"
    HOME = "HOME"
    # scrcpy reads the adb executable from this variable.
    ADB = "ADB"


class defaults:
    HOST = "127.0.0.1"
    PORT = 18766
    # Port 0 asks the kernel for a free port.
    ANY_PORT = 0
    STATE_HOME = Path(".local") / "state"
    WAYLAND_SOCKET = "wayland-0"
    SCRCPY = "scrcpy"
    STREAM_FPS = 10
    STREAM_QUALITY = 2
    STREAM_RESTART_DELAY = timedelta(seconds=2)
    # Lazy mode: stop the webcam stream after this time without frame users and page viewers.
    WEBCAM_IDLE_TIMEOUT = timedelta(minutes=5)
    WEBCAM_IDLE_CHECK = timedelta(seconds=5)
    # bench_stop answers first, then stops the page, so the page request that asked for it can end.
    PAGE_STOP_DELAY = timedelta(milliseconds=500)
    PROCESS_STOP_TIMEOUT = timedelta(seconds=5)
    CROP_JPEG_QUALITY = 95
    HISTORY_SIZE = 200
    # Images use memory, so only the most recent calls keep them.
    IMAGE_HISTORY_SIZE = 30
    SUBSCRIBER_QUEUE_SIZE = 256
    # The primary monitor keeps the calls of at most this many other MCP servers (the oldest server goes first).
    MAX_REMOTE_SERVERS = 20
    # A secondary waits this long for its primary to come back (dev monitor reload) before it serves its own page.
    PRIMARY_RESTART_GRACE = timedelta(seconds=3)
    PRIMARY_POLL = timedelta(milliseconds=250)
    # The origin label when the MCP client did not give its name in initialize.
    UNKNOWN_CLIENT = "mcp"
    SUMMARY_CHARS = 300
    SSE_KEEPALIVE = timedelta(seconds=15)


class browser:
    FIREFOX = ("firefox", "--new-window")
    FALLBACK = ("xdg-open",)


class scrcpy:
    SERIAL_FLAG = "-s"
    WINDOW_TITLE_FLAG = "--window-title"
    WINDOW_TITLE_PREFIX = "debug-devices: phone "
    FLAGS = ("--no-audio", "--stay-awake")


class stream:
    """ffmpeg arguments for a multipart JPEG stream (`mpjpeg`) from a V4L2 device on stdout."""

    INPUT_FORMAT = "v4l2"
    OUTPUT_FORMAT = "mpjpeg"
    CODEC = "mjpeg"
    LOG_LEVEL = "error"
    STDOUT = "pipe:1"
    CONTENT_LENGTH_HEADER = b"content-length"
    HEADER_SEPARATOR = b":"
    # The limit of one header line from ffmpeg; the JPEG bytes are read by length.
    READ_LIMIT = 2**16


class http:
    MJPEG_BOUNDARY = "frame"
    MJPEG_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"
    JPEG_MEDIA_TYPE = "image/jpeg"
    JSON_MEDIA_TYPE = "application/json"
    CONTENT_TYPE_HEADER = "content-type"
    SSE_MEDIA_TYPE = "text/event-stream"
    NO_CACHE: ClassVar[dict[str, str]] = {"Cache-Control": "no-store"}
    ALLOWED_HOSTS = ("127.0.0.1", "localhost")
    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
    HOST_HEADER = "host"
    ORIGIN_HEADER = "origin"
    SCHEME = "http"


class remote:
    """Frames from the monitor of another MCP process."""

    WHOAMI_PATH = "/api/whoami"
    FRAME_PATH = "/api/webcam/frame.jpg"
    INFO_PATH = "/api/webcam/info"
    STREAM_PATH = "/api/webcam/stream.mjpg"
    CROPPED_PARAM = "cropped"
    TRUE = "true"
    # ffmpeg writes "Device or resource busy" when another process reads the V4L2 device.
    BUSY_MARKER = "resource busy"
    ERROR_PREVIEW_CHARS = 300


class screen:
    """The phone screen in the page: the scrcpy server on the phone sends raw H.264, the page decodes it."""

    SERVER_PATH = Path("/usr/share/scrcpy/scrcpy-server")
    DEVICE_PATH = "/data/local/tmp/debug-devices-scrcpy-server.jar"
    SERVER_CLASS = "com.genymobile.scrcpy.Server"
    SOCKET_PREFIX = "scrcpy_"
    LOCALABSTRACT_PREFIX = "localabstract:"
    ANY_LOCAL_PORT = "tcp:0"
    MAX_SIZE = 1280
    # Seconds between key frames. A new page waits at most this long for a clean picture.
    I_FRAME_INTERVAL = 2
    SCID_BITS = 31
    CONNECT_TIMEOUT = timedelta(seconds=15)
    CONNECT_RETRY = timedelta(milliseconds=200)
    RESTART_DELAY = timedelta(seconds=3)
    READ_SIZE = 2**16
    # The frames since the last key frame. A new page gets them first. Above this size, wait for the next key frame.
    MAX_CACHE_BYTES = 32 * 2**20
    SUBSCRIBER_QUEUE_SIZE = 240
    VERSION_PATTERN = r"^scrcpy (\S+)"
    VERSION_FLAG = "--version"
    # Wire format to the page: 4-byte big-endian payload length, 1 byte kind, payload.
    LENGTH_BYTES = 4
    MEDIA_TYPE = "application/octet-stream"


class ingest:
    """A secondary MCP server sends its tool calls to the primary monitor (the page on the configured port)."""

    CALLS_PATH = "/api/ingest/calls"
    IMAGE_PATH = "/api/ingest/calls/{call_id}/images"
    TOKEN_HEADER = "X-Debug-Devices-Token"
    TOKEN_FILE_PREFIX = "ingest-"
    TOKEN_FILE_SUFFIX = ".token"
    TOKEN_BYTES = 32
    TOKEN_FILE_MODE = 0o600
    ORIGIN_PARAM = "origin"
    LABEL_PARAM = "label"
    INDEX_PARAM = "index"
    # Short: a slow or gone primary must never slow a tool call of the secondary.
    REQUEST_TIMEOUT = timedelta(seconds=1)
    SEND_ATTEMPTS = 2
    # At the stop of a secondary, its last calls still go to the primary, within this time.
    DRAIN_TIME = timedelta(seconds=1)
    DRAIN_POLL = timedelta(milliseconds=20)
    BACKOFF_START = timedelta(seconds=1)
    BACKOFF_MAX = timedelta(seconds=30)
    MAX_IMAGE_BYTES = 16 * 2**20
    MAX_EVENT_BYTES = 2**20
    IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
    REDACTED_TEXT = "(not forwarded: the user's instructions)"


class tools:
    """MCP tool names that the monitor reads results from."""

    PHONE_CONNECT = "phone_connect"
    PHONE_STATUS = "phone_status"
    PHONE_ZOOM = "phone_zoom"
    PHONE_TORCH = "phone_torch"
    PHONE_SNAPSHOT = "phone_snapshot"
    PHONE_ROTATION = "phone_rotation"
    PHONE_SNAPSHOT_ORIENTATION = "phone_snapshot_orientation"
    BOARD_OPEN = "board_open"
    BENCH_INSTRUCTIONS = "bench_instructions"
    # Their results hold the user's instructions text: other monitors get only the tool name and the status.
    REDACTED = frozenset({BENCH_INSTRUCTIONS})
    MONITOR_OPEN = "monitor_open"
    BENCH_START = "bench_start"
    BENCH_STOP = "bench_stop"
    MULTIMETER_READ = "multimeter_read"


class details:
    """Keys of `ToolCall.details`."""

    READING = "reading"
    RESULT = "result"
    SCRCPY = "scrcpy"
    SCREEN = "screen"


class labels:
    MODEL_INPUT = "image sent to the model"
    WEBCAM_FRAME = "webcam frame"
    RESULT_IMAGE = "result image"
