"""FastAPI app: live MJPEG streams, snapshot, config, recordings, static UI."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date as _date
from pathlib import Path
from typing import Optional

import shutil

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import LoginThrottle, SessionStore, hash_password, verify_password
from .camera import CameraManager, HAS_PICAMERA
from .config import AppConfig, CameraConfig, ConfigStore
from .housekeeper import Housekeeper
from .worker import CameraWorker

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


class WrenCamApp:
    def __init__(self) -> None:
        self.store = ConfigStore()
        self.cam_manager = CameraManager()
        self.workers: dict[int, CameraWorker] = {}
        self.housekeeper = Housekeeper(self.store)

    @property
    def config(self) -> AppConfig:
        return self.store.get()

    @staticmethod
    def _min_free_bytes(cfg: AppConfig) -> int:
        return cfg.min_free_mb * 1024 * 1024

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
                min_free_bytes=self._min_free_bytes(cfg),
            )
            worker.start()
            self.workers[cc.id] = worker
        self.housekeeper.start()

    def apply_app_runtime(self) -> None:
        """Push app-level settings that can change without a camera restart
        (free-space reserve, retention) to the running workers / housekeeper."""
        cfg = self.config
        min_free = self._min_free_bytes(cfg)
        for w in self.workers.values():
            w.set_min_free_bytes(min_free)
        self.housekeeper.wake()

    def stop(self) -> None:
        # 0) Stop the janitor first; it only touches the filesystem.
        try:
            self.housekeeper.request_stop()
            self.housekeeper.join(timeout=3)
        except Exception:
            logger.exception("error stopping housekeeper during shutdown")
        # 1) Stop hardware encoders first — picamera2 doesn't tolerate
        #    having start_encoder still active when the camera is closed.
        for w in self.workers.values():
            try:
                if w.recorder.is_recording():
                    w.recorder.stop()
            except Exception:
                logger.exception("error stopping recorder during shutdown")
        # 2) Signal worker threads, then join briefly. daemon=True means a
        #    stuck thread won't block process exit.
        for w in self.workers.values():
            w.request_stop()
        for w in self.workers.values():
            w.join(timeout=3)
        self.workers.clear()
        # 3) Now safe to close cameras.
        self.cam_manager.stop_all()

    def restart_camera(self, cam_id: int) -> None:
        """Apply width/height/fps changes by restarting capture and worker."""
        cfg = self.config
        cc = next((c for c in cfg.cameras if c.id == cam_id), None)
        if cc is None:
            raise KeyError(cam_id)
        worker = self.workers.get(cam_id)
        if worker is not None:
            try:
                if worker.recorder.is_recording():
                    worker.recorder.stop()
            except Exception:
                logger.exception("cam%d: recorder stop during restart failed", cam_id)
            worker.request_stop()
            worker.join(timeout=3)
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
            min_free_bytes=self._min_free_bytes(cfg),
        )
        new_worker.start()
        self.workers[cam_id] = new_worker


state = WrenCamApp()

# In-memory admin sessions. Viewing (live stream, browsing recordings) is open
# to anyone on the network; every state-changing route below depends on
# require_admin, which checks the session cookie set by /api/login.
sessions = SessionStore()
login_throttle = LoginThrottle()  # 5 failures / 5 min -> 5 min lockout, per IP
SESSION_COOKIE = "wren_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def require_admin(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not sessions.is_valid(token):
        raise HTTPException(401, "admin login required")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.start()
    try:
        yield
    finally:
        state.stop()


app = FastAPI(title="wren-cam", lifespan=lifespan)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Force browsers to revalidate the UI assets so updates aren't masked by
    an aggressive cache (notably mobile Safari). ETag/Last-Modified still allow
    a fast 304 when nothing changed."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    return {"ok": True, "picamera2": HAS_PICAMERA}


# ----- auth -----


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    key = request.client.host if request.client else "unknown"
    wait = login_throttle.seconds_until_unlocked(key)
    if wait > 0:
        retry = int(wait) + 1
        raise HTTPException(
            429,
            f"too many failed attempts; try again in {retry}s",
            headers={"Retry-After": str(retry)},
        )
    if not verify_password(body.password, state.config.admin_password):
        login_throttle.record_failure(key)
        raise HTTPException(401, "invalid password")
    login_throttle.record_success(key)
    token = sessions.create()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "admin": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    sessions.revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True, "admin": False}


@app.get("/api/session")
def session(request: Request):
    return {"admin": sessions.is_valid(request.cookies.get(SESSION_COOKIE))}


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/admin/password", dependencies=[Depends(require_admin)])
def change_password(body: PasswordBody):
    if not verify_password(body.current_password, state.config.admin_password):
        raise HTTPException(403, "current password is incorrect")
    if len(body.new_password) < 4:
        raise HTTPException(400, "new password must be at least 4 characters")
    state.store.set_admin_password(hash_password(body.new_password))
    return {"ok": True}


@app.get("/api/storage")
def storage():
    """Free / total bytes on the volume holding the recordings directory."""
    rec_dir = Path(state.config.recordings_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(str(rec_dir))
    return {"total": du.total, "used": du.used, "free": du.free}


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
    data = state.config.model_dump()
    data.pop("admin_password", None)  # never expose the password hash
    return data


class AppPatch(BaseModel):
    recordings_dir: Optional[str] = None
    stream_quality: Optional[int] = None
    stream_maxrate: Optional[int] = None
    min_free_mb: Optional[int] = None
    retention_days: Optional[int] = None


@app.patch("/api/config", dependencies=[Depends(require_admin)])
def patch_app_config(patch: AppPatch):
    fields = patch.model_dump(exclude_none=True)
    cfg = state.store.update_app(**fields)
    # Apply free-space reserve / retention changes to the live workers and
    # janitor without needing a camera restart.
    state.apply_app_runtime()
    data = cfg.model_dump()
    data.pop("admin_password", None)
    return data


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


RESTART_KEYS = ("width", "height", "framerate", "rotate_180")


@app.patch("/api/cameras/{cam_id}", dependencies=[Depends(require_admin)])
def patch_camera(cam_id: int, patch: CameraPatch):
    fields = patch.model_dump(exclude_none=True)
    old_cfg = next((c for c in state.config.cameras if c.id == cam_id), None)
    if old_cfg is None:
        raise HTTPException(404, f"camera {cam_id} not configured")
    try:
        new_cfg = state.store.update_camera(cam_id, **fields)
    except KeyError:
        raise HTTPException(404, f"camera {cam_id} not configured")

    # Only restart the camera if a hardware-level setting actually CHANGED VALUE.
    # The UI sends every field on save, so checking just the keys would trigger
    # unnecessary picamera2 open/close cycles that leak libcamera resources.
    needs_restart = any(
        getattr(old_cfg, k) != getattr(new_cfg, k) for k in RESTART_KEYS
    )
    if needs_restart:
        changed = [
            k for k in RESTART_KEYS if getattr(old_cfg, k) != getattr(new_cfg, k)
        ]
        logger.info("cam%d: restarting (changed: %s)", cam_id, ",".join(changed))
        state.restart_camera(cam_id)
    else:
        worker = state.workers.get(cam_id)
        if worker is not None:
            worker.update_config(new_cfg)
    return new_cfg.model_dump()


@app.post("/api/cameras/{cam_id}/restart", dependencies=[Depends(require_admin)])
def restart_camera(cam_id: int):
    try:
        state.restart_camera(cam_id)
    except KeyError:
        raise HTTPException(404, f"camera {cam_id} not configured")
    return {"ok": True}


@app.post("/api/cameras/{cam_id}/snapshot", dependencies=[Depends(require_admin)])
def save_snapshot(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    if not worker.has_min_free():
        raise HTTPException(507, "insufficient disk space; snapshot skipped")
    path = worker.save_snapshot()
    if path is None:
        raise HTTPException(503, "snapshot failed")
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "modified": int(st.st_mtime)}


@app.get("/snapshot/{cam_id}.jpg")
async def snapshot(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    jpeg = worker.latest_jpeg
    if not jpeg:
        # Wait for a frame off the event loop so we don't block other requests.
        jpeg = await asyncio.to_thread(worker.wait_for_frame, 3.0)
    if not jpeg:
        raise HTTPException(503, "no frame yet")
    return Response(content=jpeg, media_type="image/jpeg")


async def _mjpeg_generator_async(worker: CameraWorker, max_fps: int):
    boundary = b"--frame"
    min_dt = 1.0 / max(max_fps, 1)
    last = 0.0
    while True:
        # Each viewer awaits its own frame on the event loop. asyncio.to_thread
        # bridges to the blocking condition variable in the worker without
        # pinning a starlette threadpool worker for the connection's lifetime.
        jpeg = await asyncio.to_thread(worker.wait_for_frame, 5.0)
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
async def stream(cam_id: int):
    worker = state.workers.get(cam_id)
    if worker is None:
        raise HTTPException(404, "camera not available")
    max_fps = state.config.stream_maxrate
    return StreamingResponse(
        _mjpeg_generator_async(worker, max_fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


_MEDIA_EXTS = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# Filenames encode the capture time, e.g. cam0-20260624-143022.mp4 or
# cam0-snap-20260624-143022.jpg. Use that for date grouping when present.
_DATE_RE = re.compile(r"-(\d{4})(\d{2})(\d{2})-\d{6}")


def _recording_date(name: str, mtime: float) -> str:
    """Return the capture date as YYYY-MM-DD, from the filename or mtime."""
    m = _DATE_RE.search(name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return _date.fromtimestamp(mtime).isoformat()


def _media_entries() -> list[dict]:
    """All recordings as dicts, newest first."""
    cfg = state.config
    rec_dir = Path(cfg.recordings_dir)
    if not rec_dir.exists():
        return []
    entries = []
    for p in rec_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in _MEDIA_EXTS:
            continue
        st = p.stat()
        entries.append(
            {
                "name": p.name,
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "kind": "video" if p.suffix.lower() == ".mp4" else "photo",
                "date": _recording_date(p.name, st.st_mtime),
            }
        )
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


@app.get("/api/recordings/dates")
def list_recording_dates():
    """Distinct dates that have recordings, newest first, with counts."""
    counts: dict[str, int] = {}
    for e in _media_entries():
        counts[e["date"]] = counts.get(e["date"], 0) + 1
    return [{"date": d, "count": counts[d]} for d in sorted(counts, reverse=True)]


@app.get("/api/recordings")
def list_recordings(
    date: Optional[str] = None, limit: int = 20, offset: int = 0
):
    entries = _media_entries()
    if date:
        entries = [e for e in entries if e["date"] == date]
    total = len(entries)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    return {
        "items": entries[offset : offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


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


@app.delete("/api/recordings", dependencies=[Depends(require_admin)])
def delete_recordings(date: Optional[str] = None):
    """Delete every recording, or every recording on a given date (YYYY-MM-DD)."""
    entries = _media_entries()
    if date:
        entries = [e for e in entries if e["date"] == date]
    deleted = 0
    for e in entries:
        try:
            _safe_recording_path(e["name"]).unlink()
            deleted += 1
        except (HTTPException, FileNotFoundError):
            continue
    return {"ok": True, "deleted": deleted}


@app.delete("/api/recordings/{name}", dependencies=[Depends(require_admin)])
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
