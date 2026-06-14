"""Per-camera background worker: pulls frames, runs motion, drives recorder, exposes JPEG."""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .camera import Camera
from .config import CameraConfig
from .motion import MotionDetector
from .recorder import Recorder

logger = logging.getLogger(__name__)


@dataclass
class WorkerStatus:
    camera_id: int
    connected: bool
    recording: bool
    motion_active: bool
    last_changed_pixels: int
    current_clip: Optional[str]
    width: int
    height: int
    framerate: int


class CameraWorker(threading.Thread):
    """Background loop for one camera."""

    def __init__(
        self,
        camera: Camera,
        cfg: CameraConfig,
        recordings_dir: Path,
        stream_quality: int,
    ) -> None:
        super().__init__(daemon=True, name=f"cam{cfg.id}-worker")
        self.camera = camera
        self.cfg = cfg
        self.recordings_dir = Path(recordings_dir)
        self.stream_quality = stream_quality

        self.detector = MotionDetector(
            threshold=cfg.motion_threshold,
            noise_level=cfg.noise_level,
        )
        self.recorder = Recorder(self.recordings_dir, cfg.id)

        self._stop = threading.Event()
        self._frame_cv = threading.Condition()
        self._latest_jpeg: Optional[bytes] = None
        self._last_motion_ts: float = 0.0
        self._motion_active = False
        self._last_changed = 0

    def request_stop(self) -> None:
        self._stop.set()
        with self._frame_cv:
            self._frame_cv.notify_all()

    def update_config(self, cfg: CameraConfig) -> None:
        """Apply config changes that don't require a camera restart (motion params, focus)."""
        old_focus = (self.cfg.autofocus, self.cfg.lens_position)
        self.cfg = cfg
        self.detector.threshold = cfg.motion_threshold
        self.detector.noise_level = cfg.noise_level
        new_focus = (cfg.autofocus, cfg.lens_position)
        if new_focus != old_focus:
            self.camera.update_focus(cfg.autofocus, cfg.lens_position)

    @property
    def latest_jpeg(self) -> Optional[bytes]:
        return self._latest_jpeg

    def wait_for_frame(self, timeout: float = 5.0) -> Optional[bytes]:
        with self._frame_cv:
            self._frame_cv.wait(timeout=timeout)
            return self._latest_jpeg

    def status(self) -> WorkerStatus:
        return WorkerStatus(
            camera_id=self.cfg.id,
            connected=self.camera.is_available(),
            recording=self.recorder.is_recording(),
            motion_active=self._motion_active,
            last_changed_pixels=self._last_changed,
            current_clip=(
                self.recorder.current_path.name if self.recorder.current_path else None
            ),
            width=self.camera.width,
            height=self.camera.height,
            framerate=self.camera.framerate,
        )

    def run(self) -> None:
        from PIL import Image  # local import; not needed in tests

        target_dt = 1.0 / max(self.camera.framerate, 1)
        last_tick = 0.0
        while not self._stop.is_set():
            now = time.time()
            sleep_for = target_dt - (now - last_tick)
            if sleep_for > 0:
                time.sleep(sleep_for)
            last_tick = time.time()

            arr = self.camera.capture_array()
            if arr is None:
                time.sleep(0.5)
                continue

            try:
                img = Image.fromarray(arr)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.stream_quality)
                jpeg = buf.getvalue()
            except Exception as e:
                logger.debug("cam%d jpeg encode failed: %s", self.cfg.id, e)
                continue

            with self._frame_cv:
                self._latest_jpeg = jpeg
                self._frame_cv.notify_all()

            if self.cfg.motion_enabled:
                detected, changed = self.detector.update(arr)
                self._last_changed = changed
                gap = self.cfg.event_gap_seconds
                max_clip = self.cfg.max_clip_seconds

                if detected:
                    self._last_motion_ts = time.time()
                    if not self._motion_active:
                        self._motion_active = True
                        self.recorder.start(
                            self.camera.width, self.camera.height, self.camera.framerate,
                        )
                if self._motion_active:
                    self.recorder.write_frame(arr)
                    elapsed = self.recorder.elapsed_seconds
                    no_motion_for = time.time() - self._last_motion_ts
                    if no_motion_for >= gap or (max_clip > 0 and elapsed >= max_clip):
                        self.recorder.stop()
                        self._motion_active = False
            else:
                if self.recorder.is_recording():
                    self.recorder.stop()
                    self._motion_active = False

        if self.recorder.is_recording():
            self.recorder.stop()
