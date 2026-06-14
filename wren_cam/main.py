"""FastAPI app: live MJPEG streams, snapshot, config, recordings, static UI."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .camera import CameraManager, HAS_PICAMERA
from .config import AppConfig, CameraConfig, ConfigStore
from .worker import CameraWorker

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class WrenCamApp:
    def __init__(self) -> None:
        self.store = ConfigStore()
        self.cam_manager = CameraManager()
        self.workers: dict[int, CameraWorker] = {}

    @property
    def config(self) -> AppConfig:
        return self.store.get()

    def start(self) -> None:
        cfg = self.config
        Path(cfg.recordings_dir).mkdir(parents=True, exist_ok=True)
        self.cam_manager.start_all(cfg.cameras)
        for cc in cfg.cameras:
            cam = self.cam_manager.get(cc.id)
            if cam is None:
                logger.warning("camera %d not available; worker not started", cc.id)
                continue
            worker = CameraWorker(
                camera=cam,
                cfg=cc,
                recordings_dir=Path(cfg.recordings_dir),
                stream_quality=cfg.stream_quality,
                stream_maxrate=cfg.stream_maxrate,
            )
            worker.start()
            self.workers[cc.id] = worker

    def stop(self) -> None:
        for w in self.workers.values():
            w.request_stop()
        for w in self.workers.values():
            w.join(timeout=5)
        self.workers.clear()
        self.cam_manager.stop_all()

    def restart_camera(self, cam_id: int) -> None:
        """Apply width/height/fps changes by restarting capture and worker."""
        cfg = self.config
        cc = next((c for c in cfg.cameras if c.id == cam_id), None)
        if cc is None:
            raise KeyError(cam_id)
        worker = self.workers.get(cam_id)
        if worker is not None:
            worker.request_stop()
            worker.join(timeout=5)
        cam = self.cam_manager.get(cam_id)
        if cam is not None:
            self.cam_manager.restart(cam_id, cc)
        else:
            self.cam_manager.start_all([cc])
            cam = self.cam_manager.get(cam_id)
        if cam is None:
            self.workers.pop(cam_id, None)
            return
        new_worker = CameraWorker(
            camera=cam,
            cfg=cc,
            recordings_dir=Path(cfg.recordings_dir),
            stream_quality=cfg.stream_quality,
            stream_maxrate=cfg.stream_maxrate,
        )
        new_worker.start()
        self.workers[cam_id] = new_worker


state = WrenCamApp()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.start()
    try:
        yield
    finally:
        state.stop()


app = FastAPI(title="wren-cam", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True, "picamera2": HAS_PICAMERA}


@app.get("/api/logs")
def get_logs(lines: int = 200):
    """Return the last N lines of the rotating log file as plain text."""
    from .logging_setup import get_log_path
    path = get_log_path()
    if path is None or not path.exists():
        raise HTTPException(404, "log file not available")
    n = max(1, min(int(lines), 5000))
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 256 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
        tail = "\n".join(data.splitlines()[-n:])
        return Response(content=tail, media_type="text/plain")
    except Exception as e:
        raise HTTPException(500, f"failed to read log: {e}")


@app.get("/api/status")
def status():
    return {
        "picamera2": HAS_PICAMERA,
        "cameras": [w.status().__dict__ for w in state.workers.values()],
    }


@app.get("/api/config")
def get_config():
    return state.config.model_dump()


class AppPatch(BaseModel):
    recordings_dir: Optional[str] = None
    stream_quality: Optional[int] = None
    stream_maxrate: Optional[int] = None


@app.patch("/api/config")
def patch_app_config(patch: AppPatch):
    fields = patch.model_dump(exclude_none=True)
    cfg = state.store.update_app(**fields)
    return cfg.model_dump()


class CameraPatch(BaseModel):
    name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    framerate: Optional[int] = None
    autofocus: Optional[str] = None
    lens_position: Optional[float] = None
    rotate_180: Optional[bool] = None
    motion_enabled: Optional[bool] = None
    motion_threshold: Optional[int] = None
    noise_level: Optional[int] = None
    event_gap_seconds: Optional[int] = None
    max_clip_seconds: Optional[int] = None


@app.patch("/api/cameras/{cam_id}")
def patch_camera(cam_id: int, patch: CameraPatch):
    fields = patch.model_dump(exclude_none=True)
    try:
        new_cfg = state.store.update_camera(cam_id, **fields)
    except KeyError:
        raise HTTPException(404, f"camera {cam_id} not configured")

    needs_restart = any(k in fields for k in ("width", "height", "framerate", "rotate_180"))
    if needs_restart:
        state.restart_camera(cam_id)
    else:
        worker = state.workers.get(cam_id)
        if worker is not None:
            worker.update_config(new_cfg)
    return new_cfg.model_dump()


@app.post("/api/cameras/{cam_id}/restart")
def restart_camera(cam_id: int):
    try:
        state.restart_camera(cam_id)
    except KeyError:
        raise HTTPException(404, f"camera {cam_id} not configured")
    return {"ok": True}


@app.post("/api/cameras/{cam_id}/snapshot")
def save_snapshot(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    path = worker.save_snapshot()
    if path is None:
        raise HTTPException(503, "snapshot failed")
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "modified": int(st.st_mtime)}


@app.get("/snapshot/{cam_id}.jpg")
def snapshot(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    jpeg = worker.latest_jpeg or worker.wait_for_frame(timeout=3.0)
    if not jpeg:
        raise HTTPException(503, "no frame yet")
    return Response(content=jpeg, media_type="image/jpeg")


def _mjpeg_generator(worker: CameraWorker, max_fps: int):
    boundary = b"--frame"
    min_dt = 1.0 / max(max_fps, 1)
    last = 0.0
    while True:
        jpeg = worker.wait_for_frame(timeout=5.0)
        if jpeg is None:
            continue
        now = time.time()
        if now - last < min_dt:
            continue
        last = now
        yield (
            boundary + b"\r\n"
            + b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
            + jpeg + b"\r\n"
        )


@app.get("/stream/{cam_id}")
def stream(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    max_fps = state.config.stream_maxrate
    return StreamingResponse(
        _mjpeg_generator(worker, max_fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


_MEDIA_EXTS = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@app.get("/api/recordings")
def list_recordings():
    cfg = state.config
    rec_dir = Path(cfg.recordings_dir)
    if not rec_dir.exists():
        return []
    entries = []
    for p in rec_dir.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in _MEDIA_EXTS:
            continue
        entries.append(p)
    entries.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "modified": int(p.stat().st_mtime),
            "kind": "video" if p.suffix.lower() == ".mp4" else "photo",
        }
        for p in entries
    ]


def _safe_recording_path(name: str) -> Path:
    cfg = state.config
    rec_dir = Path(cfg.recordings_dir).resolve()
    candidate = (rec_dir / name).resolve()
    if rec_dir not in candidate.parents and candidate != rec_dir:
        raise HTTPException(400, "invalid path")
    if not candidate.is_file():
        raise HTTPException(404, "not found")
    if candidate.suffix.lower() not in _MEDIA_EXTS:
        raise HTTPException(400, "unsupported file type")
    return candidate


@app.get("/api/recordings/{name}")
def get_recording(name: str):
    path = _safe_recording_path(name)
    media_type = _MEDIA_EXTS[path.suffix.lower()]
    return FileResponse(path, media_type=media_type, filename=name)


@app.delete("/api/recordings/{name}")
def delete_recording(name: str):
    path = _safe_recording_path(name)
    path.unlink()
    return {"ok": True}


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def run() -> None:
    import uvicorn
    from .logging_setup import configure as configure_logging
    configure_logging()
    cfg = state.config
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
