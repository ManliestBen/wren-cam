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
        # Digital zoom state. _scaler_full is the maximal ScalerCrop rectangle
        # (x, y, w, h) in sensor coordinates — the coordinate space every crop
        # is computed against. It's discovered once the camera is running.
        self._scaler_full: Optional[tuple] = None
        self._zoom = 1.0
        self._center_x = 0.5
        self._center_y = 0.5
        self._rotate_180 = False

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
        zoom: float = 1.0,
        zoom_center_x: float = 0.5,
        zoom_center_y: float = 0.5,
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
                # Discover the crop coordinate space, then re-apply any saved
                # zoom so it survives restarts.
                self._scaler_full = self._read_scaler_full()
                self._zoom = max(1.0, float(zoom))
                self._center_x = min(1.0, max(0.0, float(zoom_center_x)))
                self._center_y = min(1.0, max(0.0, float(zoom_center_y)))
                self._rotate_180 = bool(rotate_180)
                self._apply_zoom()
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

    def _read_scaler_full(self) -> Optional[tuple]:
        """The maximal ScalerCrop rectangle for the current mode — the sensor
        coordinate space every crop is expressed in."""
        if self._cam is None:
            return None
        props = getattr(self._cam, "camera_properties", None) or {}
        rect = props.get("ScalerCropMaximum")
        if rect and len(rect) == 4 and rect[2] > 0 and rect[3] > 0:
            return tuple(int(v) for v in rect)
        size = props.get("PixelArraySize")
        if size and len(size) == 2 and size[0] > 0 and size[1] > 0:
            return (0, 0, int(size[0]), int(size[1]))
        return None

    def _apply_zoom(self) -> None:
        """Compute a ScalerCrop rectangle from the current zoom/center and push
        it to the ISP. Caller must hold self._lock. No-op if unsupported."""
        if self._cam is None or self._scaler_full is None:
            return
        fx, fy, fw, fh = self._scaler_full
        z = max(1.0, self._zoom)
        cx, cy = self._center_x, self._center_y
        # A 180° rotation flips the displayed image on both axes; invert the
        # requested center so a pan feels natural in the rotated view.
        if self._rotate_180:
            cx, cy = 1.0 - cx, 1.0 - cy
        crop_w = max(1, int(round(fw / z)))
        crop_h = max(1, int(round(fh / z)))
        px = fx + cx * fw - crop_w / 2.0
        py = fy + cy * fh - crop_h / 2.0
        # Keep the crop fully inside the sensor's usable rectangle.
        px = int(round(min(max(px, fx), fx + fw - crop_w)))
        py = int(round(min(max(py, fy), fy + fh - crop_h)))
        try:
            self._cam.set_controls({"ScalerCrop": (px, py, crop_w, crop_h)})
        except Exception as e:
            logger.debug("camera %d: ScalerCrop unsupported: %s", self.index, e)

    def set_zoom(
        self,
        zoom: float,
        center_x: float,
        center_y: float,
        rotate_180: Optional[bool] = None,
    ) -> None:
        with self._lock:
            self._zoom = max(1.0, float(zoom))
            self._center_x = min(1.0, max(0.0, float(center_x)))
            self._center_y = min(1.0, max(0.0, float(center_y)))
            if rotate_180 is not None:
                self._rotate_180 = bool(rotate_180)
            self._apply_zoom()

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
        zoom: float = 1.0,
        zoom_center_x: float = 0.5,
        zoom_center_y: float = 0.5,
    ) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start(
            width, height, framerate, autofocus, lens_position, rotate_180,
            zoom, zoom_center_x, zoom_center_y,
        )

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
            # "RGB888" comes back as BGR byte order; swap so PIL sees true RGB.
            if arr.ndim == 3 and arr.shape[2] == 3:
                arr = np.ascontiguousarray(arr[:, :, ::-1])
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
                cc.zoom, cc.zoom_center_x, cc.zoom_center_y,
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
        return cam.restart(
            cc.width, cc.height, cc.framerate, cc.autofocus, cc.lens_position,
            cc.rotate_180, cc.zoom, cc.zoom_center_x, cc.zoom_center_y,
        )
