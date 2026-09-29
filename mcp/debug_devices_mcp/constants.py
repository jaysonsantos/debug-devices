"""Names, defaults, and limits of the MCP server. Every literal with a meaning lives here."""

from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = REPO_ROOT / ".env"

SERVER_NAME = "debug-devices"
PROGRAM_NAME = "debug-devices-mcp"


class env:
    """Environment variable names. pydantic-settings matches them without case."""

    PREFIX = "DEBUG_DEVICES_"
    OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
    VISION_MODEL = f"{PREFIX}VISION_MODEL"
    WEBCAM = f"{PREFIX}WEBCAM"
    ADB_SERIAL = f"{PREFIX}ADB_SERIAL"
    METER_MODEL = f"{PREFIX}METER_MODEL"
    INSTRUCTIONS = f"{PREFIX}INSTRUCTIONS"
    BENCH_STATE_FILE = f"{PREFIX}BENCH_STATE_FILE"
    MAX_VOLTAGE = f"{PREFIX}MAX_VOLTAGE"
    MAX_CURRENT = f"{PREFIX}MAX_CURRENT"
    MAX_DIODE_VOLTAGE = f"{PREFIX}MAX_DIODE_VOLTAGE"
    METER_FRAME_INTERVAL = f"{PREFIX}METER_FRAME_INTERVAL"
    METER_COUNTS = f"{PREFIX}METER_COUNTS"
    METER_LOCAL_DECODER = f"{PREFIX}METER_LOCAL_DECODER"
    UI_ALLOWED_ORIGINS = f"{PREFIX}UI_ALLOWED_ORIGINS"


class defaults:
    VISION_MODEL = "openai/gpt-6-luna"
    WEBCAM = Path("/dev/video0")
    ADB = "adb"
    # Finds wireless-debugging phones when python-zeroconf finds none (discovery.py).
    AVAHI_BROWSE = "avahi-browse"
    FFMPEG = "ffmpeg"
    LOCAL_FORWARD_PORT = 18765
    MAX_PORT = 2**16 - 1
    WEBCAM_WARMUP_FRAMES = 10
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    PHONE_HTTP_TIMEOUT = timedelta(seconds=10)
    PHONE_SNAPSHOT_TIMEOUT = timedelta(seconds=30)
    # Turning the in-sensor zoom on binds the camera again and checks the vendor session: about 5 s, up to 8.4 s seen.
    PHONE_RECONFIGURE_TIMEOUT = timedelta(seconds=20)
    APP_START_TIMEOUT = timedelta(seconds=20)
    ADB_TIMEOUT = timedelta(seconds=15)
    WEBCAM_TIMEOUT = timedelta(seconds=20)
    VISION_TIMEOUT = timedelta(seconds=90)
    # Bench plausibility limits (a laptop or USB-C bench): a meter value above them is disputed.
    MAX_VOLTAGE = 30.0
    MAX_CURRENT = 10.0
    # The diode test voltage of the meter: a diode-mode reading above it is probably DC V (the dial).
    MAX_DIODE_VOLTAGE = 3.0
    METER_FRAME_INTERVAL = timedelta(seconds=1)
    # The display counts of the meter (6000 for the Proster T21D). 0: unknown, no display check.
    METER_COUNTS = 0
    POLL_INTERVAL = timedelta(milliseconds=500)


class phone:
    """Values from docs/phone-api.md."""

    DEVICE_PORT = 8765
    HOST = "127.0.0.1"
    PACKAGE = "dev.jayson.debugdevices.camera"
    ACTIVITY = ".MainActivity"
    COMPONENT = f"{PACKAGE}/{ACTIVITY}"
    PATH_HEALTH = "/v1/health"
    PATH_STATUS = "/v1/status"
    PATH_ZOOM = "/v1/zoom"
    PATH_TORCH = "/v1/torch"
    PATH_ROTATION = "/v1/rotation"
    PATH_PREVIEW = "/v1/preview"
    PATH_CAMERA = "/v1/camera"
    PATH_FOCUS = "/v1/focus"
    PATH_OVERLAY = "/v1/overlay"
    # POST /v1/overlay limits (docs/phone-api.md).
    OVERLAY_MAX_BOXES = 8
    OVERLAY_MAX_ARROWS = 4
    OVERLAY_MAX_LABEL = 32
    OVERLAY_MAX_TAG = 3
    # docs/phone-api.md: a box or arrow tag is 1-3 ASCII letters or digits (the app answers 400 otherwise).
    OVERLAY_TAG_PATTERN = r"^[A-Za-z0-9]{1,3}$"
    PATH_SNAPSHOT = "/v1/snapshot"
    # The /v1/snapshot response headers: the rotation that the app used for this still, and its app run.
    ROTATION_HEADER = "X-Rotation-Degrees"
    APP_START_HEADER = "X-App-Start-Id"
    # The app forgets its boxes and arrows this long after the call; the clients forget their copy too.
    OVERLAY_TTL = timedelta(minutes=10)


class adb:
    DEVICES = "devices"
    LONG_FLAG = "-l"
    DAEMON_LINE_PREFIX = "*"
    AM_ERROR_MARKER = "Error"
    AM_MISSING_MARKER = "does not exist"
    FORWARD = "forward"
    REMOVE_FLAG = "--remove"
    LIST_FLAG = "--list"
    SERIAL_FLAG = "-s"
    SHELL = "shell"
    TCP_PREFIX = "tcp:"
    AM_START = ("am", "start", "-n")
    DEVICES_HEADER = "List of devices attached"
    STATE_DEVICE = "device"
    TCPIP = "tcpip"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    STATE_OFFLINE = "offline"
    PAIR = "pair"
    MDNS = ("mdns", "services")
    MDNS_UNSUPPORTED_MARKER = "not supported"
    PM_PATH = ("pm", "path")
    PACKAGE_PREFIX = "package:"
    WIFI_ADDRESS = ("ip", "-f", "inet", "addr", "show", "wlan0")
    CONNECTED_MARKERS = ("connected to", "already connected to")
    PAIRED_MARKER = "Successfully paired"
    # The port for `adb tcpip`, and a device serial over Wi-Fi: <ip>:<port>.
    TCPIP_PORT = 5555


class ffmpeg:
    """Arguments for one JPEG frame from a V4L2 device to stdout."""

    INPUT_FORMAT = "v4l2"
    OUTPUT_FORMAT = "image2"
    CODEC = "mjpeg"
    QUALITY = 2
    STDOUT = "pipe:1"
    LOG_LEVEL = "error"


class openrouter:
    CHAT_COMPLETIONS_PATH = "/chat/completions"
    AUTHORIZATION_HEADER = "Authorization"
    BEARER_PREFIX = "Bearer "
    TITLE_HEADER = "X-Title"
    TITLE = SERVER_NAME
    SCHEMA_NAME = "multimeter_reading"
    JPEG_DATA_URL_PREFIX = "data:image/jpeg;base64,"
    MAX_ATTEMPTS = 2
    # gpt-6-luna is a reasoning model: reasoning tokens count against this budget before the JSON answer.
    MAX_TOKENS = 4000
    REASONING_EFFORT = "low"
    FINISH_REASON_LENGTH = "length"


JPEG_FORMAT = "jpeg"
CONTENT_TYPE_HEADER = "Content-Type"
JSON_CONTENT_TYPE = "application/json"
ERROR_BODY_PREVIEW_CHARS = 500
# One INFO line per HTTP request drowns the server log on stderr.
QUIET_LOGGERS = ("httpx", "httpcore")


class images:
    # Long edge in pixels. Larger images are scaled down by the Claude API anyway.
    DEFAULT_MAX_SIDE = 1568
    JPEG_QUALITY_STEPS = (85, 75, 65)
    JPEG_MODE = "RGB"
    PIL_JPEG_FORMAT = "JPEG"
