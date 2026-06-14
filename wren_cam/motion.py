"""Frame-difference motion detection (numpy only, no OpenCV)."""

from __future__ import annotations

from typing import Optional

import numpy as np


def _to_grayscale(arr: np.ndarray) -> np.ndarray:
    if arr.ndim == 3:
        return np.dot(arr[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
    return arr.astype(np.uint8)


class MotionDetector:
    def __init__(
        self,
        threshold: int = 1500,
        noise_level: int = 32,
        minimum_motion_frames: int = 1,
    ) -> None:
        self.threshold = threshold
        self.noise_level = noise_level
        self.minimum_motion_frames = minimum_motion_frames
        self._reference: Optional[np.ndarray] = None
        self._motion_count = 0
        self._last_changed = 0

    def update(self, frame: np.ndarray) -> tuple[bool, int]:
        gray = _to_grayscale(frame)
        if self._reference is None:
            self._reference = gray
            return False, 0
        diff = np.abs(np.int16(gray) - np.int16(self._reference))
        changed = int(np.count_nonzero(diff >= self.noise_level))
        self._last_changed = changed
        triggered = changed >= self.threshold
        if triggered:
            self._motion_count += 1
            # Slow reference update so we don't lose tracking
            self._reference = (
                0.95 * self._reference.astype(np.float32)
                + 0.05 * gray.astype(np.float32)
            ).astype(np.uint8)
        else:
            self._motion_count = 0
            self._reference = gray
        return (self._motion_count >= self.minimum_motion_frames and triggered, changed)

    def reset(self) -> None:
        self._reference = None
        self._motion_count = 0

    @property
    def last_changed(self) -> int:
        return self._last_changed
