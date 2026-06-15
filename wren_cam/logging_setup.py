"""Centralized logging: stderr (journal) + rotating file + uncaught-exception capture."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Optional

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_LOG_PATH = "./wren-cam.log"
_log_path: Optional[Path] = None


class _FlushingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Flushes after every emit so a system freeze can't lose recent log lines."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        super().emit(record)
        try:
            if self.stream:
                self.stream.flush()
        except Exception:
            pass


def get_log_path() -> Optional[Path]:
    return _log_path


def configure(level: str = "INFO", log_file: Optional[str] = None) -> Optional[Path]:
    """Set up root logger, file rotation, and process-wide exception hooks."""
    global _log_path
    log_file = log_file or os.environ.get("WREN_CAM_LOG", _DEFAULT_LOG_PATH)

    formatter = logging.Formatter(_FORMAT)
    handlers: list[logging.Handler] = []

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(formatter)
    handlers.append(stderr_h)

    path: Optional[Path] = None
    if log_file:
        try:
            path = Path(log_file).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            file_h = _FlushingRotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_h.setFormatter(formatter)
            handlers.append(file_h)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"wren-cam: file logging disabled ({e})\n")
            path = None

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )
    _log_path = path

    log = logging.getLogger("wren_cam.uncaught")

    def _sys_excepthook(exc_type, exc, tb):
        log.error("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_excepthook(args: threading.ExceptHookArgs):
        tname = args.thread.name if args.thread else "?"
        log.error(
            "uncaught exception in thread %s", tname,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook

    if path is not None:
        log.info("logging to %s", path)
    return path
