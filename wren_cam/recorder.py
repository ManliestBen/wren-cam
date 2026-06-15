"""Async MP4 recorder.

The capture loop must never block. Frames are dropped into a bounded queue
and a dedicated writer thread feeds them to ffmpeg. If ffmpeg falls behind,
new frames are dropped (counted, logged) instead of stalling the worker.
ffmpeg's stderr is captured in another thread so its diagnostics end up
in our log file.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class Recorder:
    MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB
    QUEUE_MAX = 30                       # ~2 seconds at 15 fps; drops beyond this
    STATS_INTERVAL = 5.0                 # seconds between progress logs

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

        self._queue: Optional[queue.Queue[bytes]] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_writer = threading.Event()
        self._frames_written = 0
        self._frames_dropped = 0

    # ----- helpers -----

    @staticmethod
    def ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(str(self.recordings_dir)).free
        except Exception as e:  # noqa: BLE001
            logger.warning("disk_usage check failed: %s", e)
            return -1

    # ----- lifecycle -----

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
            "-hide_banner", "-nostdin",
            "-loglevel", "warning",
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
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            logger.exception("failed to start ffmpeg: %s", e)
            self._proc = None
            return None

        self._path = out
        self._width = width
        self._height = height
        self._fps = fps
        self._started_at = datetime.now()
        self._frames_written = 0
        self._frames_dropped = 0
        self._queue = queue.Queue(maxsize=self.QUEUE_MAX)
        self._stop_writer.clear()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name=f"cam{self.camera_id}-rec-writer", daemon=True,
        )
        self._writer_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"cam{self.camera_id}-rec-stderr", daemon=True,
        )
        self._stderr_thread.start()
        logger.info(
            "recording started: %s (%dx%d @ %d fps)", out, width, height, fps,
        )
        return out

    def stop(self) -> Optional[Path]:
        with self._lock:
            proc = self._proc
            path = self._path
            q = self._queue
            if proc is None:
                return None

            self._stop_writer.set()
            # Wake the writer thread so it sees the stop flag.
            if q is not None:
                try:
                    q.put_nowait(b"")
                except queue.Full:
                    pass

        # Wait for writer to drain & close stdin.
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)

        # Now wait for ffmpeg to finish encoding.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("cam%d: ffmpeg did not exit in 10s; killing", self.camera_id)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as e:  # noqa: BLE001
                logger.warning("cam%d: ffmpeg kill failed: %s", self.camera_id, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("cam%d: ffmpeg wait failed: %s", self.camera_id, e)

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)

        rc = proc.returncode if proc else None
        wrote = self._frames_written
        dropped = self._frames_dropped
        self._cleanup_state()
        logger.info(
            "recording stopped: %s (ffmpeg exit=%s, %d frames written, %d dropped)",
            path, rc, wrote, dropped,
        )
        return path

    def _cleanup_state(self) -> None:
        self._proc = None
        self._path = None
        self._started_at = None
        self._queue = None
        self._writer_thread = None
        self._stderr_thread = None

    # ----- input from worker -----

    def write_frame(self, frame: np.ndarray) -> None:
        """Non-blocking; drops frames when the writer can't keep up."""
        if self._proc is None or self._queue is None:
            return
        if frame.shape != (self._height, self._width, 3) or frame.dtype != np.uint8:
            # Quietly skip frames whose shape changed mid-recording.
            self._frames_dropped += 1
            return
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        try:
            self._queue.put_nowait(frame.tobytes())
        except queue.Full:
            self._frames_dropped += 1

    # ----- background threads -----

    def _writer_loop(self) -> None:
        import time
        proc = self._proc
        q = self._queue
        if proc is None or proc.stdin is None or q is None:
            return
        last_stats = time.time()
        try:
            while True:
                if self._stop_writer.is_set() and q.empty():
                    break
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if not data:
                    # Sentinel from stop(); flush remaining and exit.
                    if self._stop_writer.is_set():
                        break
                    continue
                try:
                    proc.stdin.write(data)
                except (BrokenPipeError, ValueError, OSError) as e:
                    logger.warning(
                        "cam%d: ffmpeg stdin write failed (%s); ending clip",
                        self.camera_id, e,
                    )
                    break
                self._frames_written += 1

                now = time.time()
                if now - last_stats >= self.STATS_INTERVAL:
                    qsize = q.qsize()
                    logger.info(
                        "cam%d recording: %d frames written, %d dropped, queue=%d",
                        self.camera_id, self._frames_written, self._frames_dropped, qsize,
                    )
                    last_stats = now
        except Exception:
            logger.exception("cam%d writer thread crashed", self.camera_id)
        finally:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # ffmpeg's -loglevel warning means anything emitted is worth seeing.
                logger.warning("cam%d ffmpeg: %s", self.camera_id, line)
        except Exception:
            logger.exception("cam%d ffmpeg stderr reader crashed", self.camera_id)

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
        return self._frames_written

    @property
    def frames_dropped(self) -> int:
        return self._frames_dropped
