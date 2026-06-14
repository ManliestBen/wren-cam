"""Persistent JSON config for wren-cam."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class CameraConfig(BaseModel):
    id: int = Field(ge=0, le=1)
    name: str = "Wren Cam"
    width: int = Field(default=1280, ge=320, le=4608)
    height: int = Field(default=720, ge=240, le=2592)
    framerate: int = Field(default=15, ge=2, le=60)
    autofocus: Literal["continuous", "manual"] = "continuous"
    lens_position: float = Field(default=0.0, ge=0.0, le=15.0)
    motion_enabled: bool = True
    motion_threshold: int = Field(default=1500, ge=1, le=10_000_000)
    noise_level: int = Field(default=32, ge=1, le=255)
    event_gap_seconds: int = Field(default=30, ge=1, le=3600)
    max_clip_seconds: int = Field(default=300, ge=0, le=86400)


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    recordings_dir: str = "./recordings"
    stream_quality: int = Field(default=80, ge=1, le=100)
    stream_maxrate: int = Field(default=15, ge=1, le=60)
    cameras: list[CameraConfig] = Field(default_factory=lambda: [CameraConfig(id=0)])


DEFAULT_PATH = Path(os.environ.get("WREN_CAM_CONFIG", "./config.json"))


class ConfigStore:
    """Load/save AppConfig as JSON, thread-safe."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> AppConfig:
        if not self.path.exists():
            cfg = AppConfig()
            self._write(cfg)
            return cfg
        with self.path.open("r") as f:
            return AppConfig.model_validate(json.load(f))

    def _write(self, cfg: AppConfig) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(cfg.model_dump(), f, indent=2)
        tmp.replace(self.path)

    def get(self) -> AppConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def update_app(self, **fields) -> AppConfig:
        with self._lock:
            data = self._config.model_dump()
            for k, v in fields.items():
                if k in data and k != "cameras":
                    data[k] = v
            self._config = AppConfig.model_validate(data)
            self._write(self._config)
            return self._config.model_copy(deep=True)

    def update_camera(self, camera_id: int, **fields) -> CameraConfig:
        with self._lock:
            data = self._config.model_dump()
            found = False
            for cam in data["cameras"]:
                if cam["id"] == camera_id:
                    for k, v in fields.items():
                        if k in cam and k != "id":
                            cam[k] = v
                    found = True
                    break
            if not found:
                raise KeyError(f"camera {camera_id} not in config")
            self._config = AppConfig.model_validate(data)
            self._write(self._config)
            return next(c for c in self._config.cameras if c.id == camera_id).model_copy(deep=True)
