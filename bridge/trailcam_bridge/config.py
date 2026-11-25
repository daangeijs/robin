"""Configuration from environment variables (systemd EnvironmentFile)."""
import os
from dataclasses import dataclass

DEFAULT_SERIAL = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"


@dataclass
class Config:
    serial_port: str = DEFAULT_SERIAL
    baud: int = 921600
    data_dir: str = "/var/lib/trailcam-bridge"
    tz: str = "Europe/Amsterdam"
    http_host: str = "0.0.0.0"
    http_port: int = 8090
    token: str = ""
    camera_ip: str = "192.168.43.1"
    session_budget: float = 720.0             # s; ESP gives up after 15 min without STOP
    video_idle: float = 6.0                   # s; serial relay adds latency/retransmit gaps
    min_video_seconds: float = 2.0
    log_level: str = "INFO"

    @property
    def clips_dir(self):
        return os.path.join(self.data_dir, "clips")

    @classmethod
    def from_env(cls, env=os.environ):
        c = cls()
        g = env.get
        c.serial_port = g("TRAILCAM_SERIAL", c.serial_port)
        c.baud = int(g("TRAILCAM_BAUD", c.baud))
        c.data_dir = g("TRAILCAM_DATA_DIR", c.data_dir)
        c.tz = g("TRAILCAM_TZ", c.tz)
        c.http_host = g("TRAILCAM_HTTP_HOST", c.http_host)
        c.http_port = int(g("TRAILCAM_HTTP_PORT", c.http_port))
        c.token = g("TRAILCAM_TOKEN", c.token)
        c.camera_ip = g("TRAILCAM_CAMERA_IP", c.camera_ip)
        c.session_budget = float(g("TRAILCAM_SESSION_BUDGET", c.session_budget))
        c.video_idle = float(g("TRAILCAM_VIDEO_IDLE", c.video_idle))
        c.min_video_seconds = float(g("TRAILCAM_MIN_VIDEO_SECONDS", c.min_video_seconds))
        c.log_level = g("TRAILCAM_LOG_LEVEL", c.log_level)
        return c
