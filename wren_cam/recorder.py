"""MP4 recorder using the Raspberry Pi 5's hardware H.264 encoder.

Picamera2 wires this directly into the camera pipeline: frames go from the
sensor → ISP → hardware encoder → MP4 muxer without ever passing through
Python. CPU and memory pressure from recording are near-zero, which is
what we need to keep the Pi alive.

ffmpeg is still required, but only to mux the H.264 stream into an MP4
container — no encoding work for it to do.
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from picamera2.encoders import H264Encoder  # type: ignore
    from picamera2.outputs import FfmpegOutput  # type: ignore
    HAS_HW_ENCODER = True
except ImportError:
    H264Encoder = None  # type: ignore
    FfmpegOutput = None  # type: ignore
    HAS_HW_ENCODER = False


class Recorder:
    MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB
    DEFAULT_BITRATE = 5_000_000          # 5 Mbps; fine for 720p/1080p15

    def __init__(self, recordings_dir: Path, camera_id: int) -> None:
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._picam2: Optional[Any] = None
        self._encoder: Optional[Any] = None
        self._output: Optional[Any] = None
        self._path: Optional[Path] = None
        self._started_at: Optional[datetime] = None

    # ----- helpers -----

    @staticmethod
    def ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    def is_recording(self) -> bool:
        return self._encoder is not None

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(str(self.recordings_dir)).free
        except Exception as e:  # noqa: BLE001
            logger.warning("disk_usage check failed: %s", e)
            return -1

    # ----- lifecycle -----

    def start(
        self,
        picam2: Any,
        width: int,
        height: int,
        fps: int,
        bitrate: int = DEFAULT_BITRATE,
    ) -> Optional[Path]:
        """Start hardware H.264 recording on the given Picamera2 instance.

        width/height/fps are accepted for logging only — the hardware encoder
        uses whatever the camera is currently configured for.
        """
        with self._lock:
            if self._encoder is not None:
                return self._path
            if not HAS_HW_ENCODER:
                logger.error("picamera2.encoders not available; cannot record")
                return None
            if not self.ffmpeg_available():
                logger.error("ffmpeg not found in PATH; FfmpegOutput requires it")
                return None
            if picam2 is None:
                logger.error("no picamera2 instance provided to recorder")
                return None
            free = self._free_bytes()
            if 0 <= free < self.MIN_FREE_BYTES:
                logger.warning(
                    "cam%d: skipping recording, only %.1f MB free in %s",
                    self.camera_id, free / 1024 / 1024, self.recordings_dir,
                )
                return None

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_path = self.recordings_dir / f"cam{self.camera_id}-{ts}.mp4"
            try:
                encoder = H264Encoder(bitrate=bitrate)
                output = FfmpegOutput(str(out_path))
                picam2.start_encoder(encoder, output)
            except Exception:
                logger.exception("cam%d: hardware encoder start failed", self.camera_id)
                return None

            self._picam2 = picam2
            self._encoder = encoder
            self._output = output
            self._path = out_path
            self._started_at = datetime.now()
            logger.info(
                "recording started (hw encoder): %s (%dx%d @ %d fps, %d kbps)",
                out_path, width, height, fps, bitrate // 1000,
            )
            return out_path

    def stop(self) -> Optional[Path]:
        with self._lock:
            if self._encoder is None or self._picam2 is None:
                return None
            path = self._path
            try:
                self._picam2.stop_encoder()
            except Exception:
                logger.exception("cam%d: stop_encoder failed", self.camera_id)
            self._encoder = None
            self._output = None
            self._picam2 = None
            self._path = None
            self._started_at = None
            logger.info("recording stopped: %s", path)
            return path

    # ----- worker compatibility (no-op; hardware path doesn't need frames pushed in) -----

    def write_frame(self, frame: Any) -> None:  # noqa: ARG002
        return

    # ----- diagnostics -----

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    @property
    def current_path(self) -> Optional[Path]:
        return self._path

    @property
    def frames_written(self) -> int:
        # Hardware encoder bypasses Python; we don't have a frame count here.
        return 0

    @property
    def frames_dropped(self) -> int:
        return 0
