"""Names, defaults, and limits of the monitor window, the webcam stream, and scrcpy."""

from datetime import timedelta
from pathlib import Path
from typing import ClassVar

STATE_DIR_NAME = "debug-devices"
# `/api/whoami` answers with this name. Other MCP processes check it before they read frames from the port.
APP_NAME = "debug-devices-monitor"
SETTINGS_FILE_NAME = "ui-settings.json"
SCRCPY_LOG_FILE_NAME = "scrcpy.log"
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = "index.html"


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
    PROCESS_STOP_TIMEOUT = timedelta(seconds=5)
    CROP_JPEG_QUALITY = 95
    HISTORY_SIZE = 200
    # Images use memory, so only the most recent calls keep them.
    IMAGE_HISTORY_SIZE = 30
    SUBSCRIBER_QUEUE_SIZE = 256
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


class tools:
    """MCP tool names that the monitor reads results from."""

    PHONE_CONNECT = "phone_connect"
    PHONE_STATUS = "phone_status"
    PHONE_ZOOM = "phone_zoom"
    PHONE_TORCH = "phone_torch"
    PHONE_SNAPSHOT = "phone_snapshot"
    PHONE_ROTATION = "phone_rotation"
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
