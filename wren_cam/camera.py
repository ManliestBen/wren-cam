"""Picamera2 wrapper. Manages 1-2 CSI cameras, exposes JPEG frames + numpy arrays."""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2  # type: ignore
    from libcamera import controls as libc_controls  # type: ignore
    from libcamera import Transform  # type: ignore
    HAS_PICAMERA = True
except ImportError:
    Picamera2 = None  # type: ignore
    libc_controls = None  # type: ignore
    Transform = None  # type: ignore
    HAS_PICAMERA = False


class Camera:
    """One Picamera2 instance. Configuration changes require restart()."""

    def __init__(self, index: int) -> None:
        self.index = index
        self._cam = None
        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_jpeg_ts: float = 0.0
        self.width = 0
        self.height = 0
        self.framerate = 0

    def is_available(self) -> bool:
        return self._cam is not None

    def start(
        self,
        width: int,
        height: int,
        framerate: int,
        autofocus: str = "continuous",
        lens_position: float = 0.0,
        rotate_180: bool = False,
    ) -> bool:
        if not HAS_PICAMERA:
            logger.warning("picamera2 unavailable; camera %d disabled", self.index)
            return False
        with self._lock:
            try:
                self._cam = Picamera2(self.index)
                kwargs = dict(
                    main={"size": (width, height), "format": "RGB888"},
                    controls={"FrameRate": float(framerate)},
                )
                if rotate_180:
                    kwargs["transform"] = Transform(hflip=True, vflip=True)
                cfg = self._cam.create_video_configuration(**kwargs)
                self._cam.configure(cfg)
                self._apply_focus(autofocus, lens_position)
                self._cam.start()
                self.width = width
                self.height = height
                self.framerate = framerate
                logger.info("camera %d started %dx%d @ %dfps", self.index, width, height, framerate)
                return True
            except Exception as e:
                logger.exception("camera %d failed to start: %s", self.index, e)
                self._cam = None
                return False

    def _apply_focus(self, autofocus: str, lens_position: float) -> None:
        if self._cam is None or libc_controls is None:
            return
        try:
            if autofocus == "manual":
                self._cam.set_controls({
                    "AfMode": libc_controls.AfModeEnum.Manual,
                    "LensPosition": float(lens_position),
                })
            else:
                self._cam.set_controls({"AfMode": libc_controls.AfModeEnum.Continuous})
        except Exception as e:
            logger.debug("camera %d: focus control unsupported: %s", self.index, e)

    def update_focus(self, autofocus: str, lens_position: float) -> None:
        with self._lock:
            self._apply_focus(autofocus, lens_position)

    def stop(self) -> None:
        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.stop()
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None

    def restart(
        self,
        width: int,
        height: int,
        framerate: int,
        autofocus: str,
        lens_position: float,
        rotate_180: bool = False,
    ) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start(width, height, framerate, autofocus, lens_position, rotate_180)

    def capture_array(self) -> Optional[np.ndarray]:
        cam = self._cam
        if cam is None:
            return None
        try:
            return cam.capture_array("main")
        except Exception as e:
            logger.debug("camera %d capture_array failed: %s", self.index, e)
            return None

    def capture_jpeg(self, quality: int = 80) -> Optional[bytes]:
        arr = self.capture_array()
        if arr is None:
            return None
        try:
            from PIL import Image
            img = Image.fromarray(arr)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            self._latest_jpeg = data
            self._latest_jpeg_ts = time.time()
            return data
        except Exception as e:
            logger.debug("camera %d jpeg encode failed: %s", self.index, e)
            return None

    @property
    def latest_jpeg(self) -> Optional[bytes]:
        return self._latest_jpeg

    @property
    def picam2(self):
        """Underlying Picamera2 instance — used by the hardware H.264 recorder."""
        return self._cam


class CameraManager:
    """Owns all configured cameras."""

    def __init__(self) -> None:
        self.cameras: dict[int, Camera] = {}

    def start_all(self, configs: list) -> None:
        for i, cc in enumerate(configs):
            if i > 0:
                time.sleep(5.0)
            cam = Camera(cc.id)
            ok = cam.start(
                cc.width, cc.height, cc.framerate,
                cc.autofocus, cc.lens_position,
                cc.rotate_180,
            )
            if ok:
                self.cameras[cc.id] = cam

    def get(self, cam_id: int) -> Optional[Camera]:
        return self.cameras.get(cam_id)

    def stop_all(self) -> None:
        for cam in self.cameras.values():
            cam.stop()
        self.cameras.clear()

    def restart(self, cam_id: int, cc) -> bool:
        cam = self.cameras.get(cam_id)
        if cam is None:
            cam = Camera(cc.id)
            self.cameras[cam_id] = cam
        return cam.restart(cc.width, cc.height, cc.framerate, cc.autofocus, cc.lens_position, cc.rotate_180)
