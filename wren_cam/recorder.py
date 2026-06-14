"""MP4 recorder. Pipes raw frames into ffmpeg when motion is active."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class Recorder:
    """One ffmpeg process per active recording. Frames pushed via write_frame()."""

    def __init__(self, recordings_dir: Path, camera_id: int) -> None:
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.camera_id = camera_id
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._lock = threading.Lock()
        self._width = 0
        self._height = 0
        self._fps = 0
        self._started_at: Optional[datetime] = None

    MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB; refuse to start a clip below this

    @staticmethod
    def ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    def is_recording(self) -> bool:
        return self._proc is not None

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(str(self.recordings_dir)).free
        except Exception as e:
            logger.warning("disk_usage check failed: %s", e)
            return -1  # treat unknown as OK; don't block recording

    def start(self, width: int, height: int, fps: int) -> Optional[Path]:
        if self._proc is not None:
            return self._path
        if not self.ffmpeg_available():
            logger.error("ffmpeg not found in PATH; cannot record")
            return None
        free = self._free_bytes()
        if 0 <= free < self.MIN_FREE_BYTES:
            logger.warning(
                "cam%d: skipping recording, only %.1f MB free in %s",
                self.camera_id, free / 1024 / 1024, self.recordings_dir,
            )
            return None
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = self.recordings_dir / f"cam{self.camera_id}-{ts}.mp4"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            self._path = out
            self._width = width
            self._height = height
            self._fps = fps
            self._started_at = datetime.now()
            logger.info("recording started: %s", out)
            return out
        except Exception as e:
            logger.exception("failed to start ffmpeg: %s", e)
            self._proc = None
            return None

    def write_frame(self, frame: np.ndarray) -> None:
        with self._lock:
            if self._proc is None or self._proc.stdin is None:
                return
            if frame.shape[1] != self._width or frame.shape[0] != self._height:
                return
            try:
                self._proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, ValueError, OSError) as e:
                logger.warning("recorder pipe failed (%s); ending clip", e)
                self._cleanup_proc()

    def stop(self) -> Optional[Path]:
        with self._lock:
            if self._proc is None:
                return None
            path = self._path
            try:
                if self._proc.stdin:
                    try:
                        self._proc.stdin.close()
                    except Exception:
                        pass
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not exit in 10s; killing")
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                except Exception as e:
                    logger.warning("ffmpeg kill failed: %s", e)
            except Exception as e:
                logger.warning("ffmpeg stop issue: %s", e)
            self._cleanup_proc()
            logger.info("recording stopped: %s", path)
            return path

    def _cleanup_proc(self) -> None:
        """Drop process refs and reset state. Caller holds the lock (or is single-threaded)."""
        proc = self._proc
        self._proc = None
        self._path = None
        self._started_at = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    @property
    def current_path(self) -> Optional[Path]:
        return self._path
