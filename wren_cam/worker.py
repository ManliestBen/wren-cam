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
    measured_fps: float


class CameraWorker(threading.Thread):
    """Background loop for one camera."""

    def __init__(
        self,
        camera: Camera,
        cfg: CameraConfig,
        recordings_dir: Path,
        stream_quality: int,
        stream_maxrate: int,
    ) -> None:
        super().__init__(daemon=True, name=f"cam{cfg.id}-worker")
        self.camera = camera
        self.cfg = cfg
        self.recordings_dir = Path(recordings_dir)
        self.stream_quality = stream_quality
        self.stream_maxrate = max(1, stream_maxrate)

        self.detector = MotionDetector(
            threshold=cfg.motion_threshold,
            noise_level=cfg.noise_level,
        )
        self.recorder = Recorder(self.recordings_dir, cfg.id)

        self._stop = threading.Event()
        self._frame_cv = threading.Condition()
        self._latest_jpeg: Optional[bytes] = None
        self._last_jpeg_ts: float = 0.0
        self._last_motion_ts: float = 0.0
        self._motion_active = False
        self._last_changed = 0
        self._measured_fps: float = float(camera.framerate)

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
            measured_fps=round(self._measured_fps, 2),
        )

    def _encode_jpeg(self, arr, quality: Optional[int] = None) -> Optional[bytes]:
        from PIL import Image
        try:
            img = Image.fromarray(arr)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality or self.stream_quality)
            return buf.getvalue()
        except Exception as e:
            logger.debug("cam%d jpeg encode failed: %s", self.cfg.id, e)
            return None

    def save_snapshot(self, quality: int = 95) -> Optional[Path]:
        """Capture a fresh high-quality JPEG and save it to the recordings dir."""
        arr = self.camera.capture_array()
        if arr is None:
            return None
        jpeg = self._encode_jpeg(arr, quality=quality)
        if jpeg is None:
            return None
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.recordings_dir / f"cam{self.cfg.id}-snap-{ts}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
        logger.info("snapshot saved: %s", path)
        return path

    def run(self) -> None:
        target_dt = 1.0 / max(self.camera.framerate, 1)
        stream_dt = 1.0 / max(self.stream_maxrate, 1)
        state = {"prev_capture_ts": 0.0, "last_tick": 0.0, "consecutive_failures": 0}

        while not self._stop.is_set():
            try:
                self._tick(target_dt, stream_dt, state)
                state["consecutive_failures"] = 0
            except Exception:
                state["consecutive_failures"] += 1
                logger.exception(
                    "cam%d tick failed (%d in a row); continuing",
                    self.cfg.id, state["consecutive_failures"],
                )
                # Exponential backoff capped at 10 s so we don't busy-spin on persistent errors.
                time.sleep(min(10.0, 0.5 * state["consecutive_failures"]))

        try:
            if self.recorder.is_recording():
                self.recorder.stop()
        except Exception:
            logger.exception("cam%d: recorder stop on exit failed", self.cfg.id)

    def _tick(self, target_dt: float, stream_dt: float, state: dict) -> None:
        now = time.time()
        sleep_for = target_dt - (now - state["last_tick"])
        if sleep_for > 0:
            time.sleep(sleep_for)
        state["last_tick"] = time.time()

        arr = self.camera.capture_array()
        if arr is None:
            time.sleep(0.5)
            return

        now = time.time()
        prev = state["prev_capture_ts"]
        if prev > 0:
            dt = now - prev
            if dt > 0:
                self._measured_fps = 0.9 * self._measured_fps + 0.1 * (1.0 / dt)
        state["prev_capture_ts"] = now

        if now - self._last_jpeg_ts >= stream_dt:
            jpeg = self._encode_jpeg(arr)
            if jpeg is not None:
                with self._frame_cv:
                    self._latest_jpeg = jpeg
                    self._frame_cv.notify_all()
                self._last_jpeg_ts = now

        if not self.cfg.motion_enabled:
            if self.recorder.is_recording():
                self.recorder.stop()
                self._motion_active = False
            return

        try:
            detected, changed = self.detector.update(arr)
        except Exception:
            logger.exception("cam%d motion detector failed", self.cfg.id)
            return
        self._last_changed = changed

        if detected:
            self._last_motion_ts = time.time()
            if not self._motion_active:
                fps_for_recording = max(1, int(round(self._measured_fps)))
                started = self.recorder.start(
                    self.camera.width, self.camera.height, fps_for_recording,
                )
                self._motion_active = started is not None

        if self._motion_active and self.recorder.is_recording():
            self.recorder.write_frame(arr)
            elapsed = self.recorder.elapsed_seconds
            no_motion_for = time.time() - self._last_motion_ts
            if (
                no_motion_for >= self.cfg.event_gap_seconds
                or (self.cfg.max_clip_seconds > 0 and elapsed >= self.cfg.max_clip_seconds)
            ):
                self.recorder.stop()
                self._motion_active = False
        elif self._motion_active and not self.recorder.is_recording():
            # Recorder died mid-clip (broken pipe, disk full). Reset state.
            self._motion_active = False
