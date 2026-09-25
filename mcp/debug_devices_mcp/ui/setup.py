"""Build the monitor from the server settings, and connect it to the services."""

from debug_devices_mcp.config import Settings
from debug_devices_mcp.phone_api import CameraStatus
from debug_devices_mcp.phone_screen import PhoneScreen, PhoneScreenOptions
from debug_devices_mcp.process import SubprocessRunner
from debug_devices_mcp.remote_webcam import RemoteMonitor, SharedWebcam
from debug_devices_mcp.scrcpy import ScrcpyLauncher, ScrcpyOptions
from debug_devices_mcp.server import Services
from debug_devices_mcp.ui.constants import SCRCPY_LOG_FILE_NAME
from debug_devices_mcp.ui.forward import CallForwarder
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions, MonitorParts
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore, state_dir
from debug_devices_mcp.webcam_stream import StreamOptions, WebcamStream


def build_monitor(settings: Settings, services: Services) -> Monitor:
    """The monitor owns the webcam from now on: the webcam tools take their frames from its stream.

    Nothing starts here. The server start hook (`Monitor.start`) and the first tool calls start the parts.
    """
    directory = state_dir()
    start = EffectiveSettings(
        vision_model=settings.vision_model,
        webcam_warmup_frames=settings.webcam_warmup_frames,
        webcam_crop=settings.webcam_crop,
    )
    scrcpy = None
    if settings.scrcpy_window:
        scrcpy = ScrcpyLauncher(
            ScrcpyOptions(
                scrcpy_path=settings.scrcpy_path,
                adb_path=settings.adb_path,
                log_path=directory / SCRCPY_LOG_FILE_NAME,
            )
        )
    stream_options = StreamOptions(
        ffmpeg_path=settings.ffmpeg_path,
        device=settings.webcam,
        warmup_frames=settings.webcam_warmup_frames,
        timeout=settings.webcam_timeout,
    )
    stream = WebcamStream(stream_options)

    async def read_status() -> CameraStatus:
        # The status poll also sees an app restart (other preview flips) and sends the flips again.
        return await services.preview_sync.ensure(await services.phone.status())

    async def remove_forward(serial: str) -> None:
        await services.adb.remove_forward(serial, settings.local_forward_port)

    shared = SharedWebcam(stream, RemoteMonitor(settings.ui_port, settings.webcam_timeout))
    monitor = Monitor(
        start,
        SettingsStore.in_dir(directory),
        MonitorOptions(
            port=settings.ui_port,
            open_browser=settings.ui_open_browser,
            start=settings.ui_start,
            webcam_idle_timeout=settings.webcam_idle_timeout,
        ),
        MonitorParts(
            stream=stream,
            scrcpy=scrcpy,
            shared=shared,
            status_reader=read_status,
            forward_remover=remove_forward,
            orientation=services.orientation,
        ),
    )
    monitor.bus.update_phone(orientation=services.orientation.current)
    services.orientation.add_listener(monitor.orientation_changed)
    if settings.phone_screen:
        monitor.screen = PhoneScreen(
            PhoneScreenOptions(
                adb_path=settings.adb_path,
                server_path=settings.scrcpy_server_path,
                version=settings.scrcpy_server_version,
                scrcpy_path=settings.scrcpy_path,
                max_size=settings.phone_screen_max_size,
                adb_timeout=settings.adb_timeout,
                log_path=directory / SCRCPY_LOG_FILE_NAME,
            ),
            SubprocessRunner(),
            on_state=monitor.screen_changed,
        )
    # When another server has the page on the configured port, this server sends its calls there.
    monitor.forwarder = CallForwarder(
        monitor.bus, RemoteMonitor(settings.ui_port, settings.webcam_timeout), monitor.is_secondary, monitor.origin
    )
    stream.set_crop_provider(monitor.crop)
    shared.set_start_local(monitor.start_stream)
    services.webcam = monitor.frame_source(shared)
    services.phone.snapshot = monitor.phone_snapshot_recorder(services.phone.snapshot)  # type: ignore[method-assign]

    def use_model(effective: EffectiveSettings) -> None:
        services.vision.model = effective.vision_model

    monitor.add_settings_listener(use_model)
    return monitor
