"""Background janitor thread: enforces the 'delete recordings after N days'
retention policy. Reads config live on each sweep, so changes take effect
without a restart. Deletion is based on file mtime, which — because clips and
snapshots are written once and never touched — equals their capture time.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MEDIA_EXTS = {".mp4", ".jpg", ".jpeg"}


class Housekeeper(threading.Thread):
    def __init__(self, store, interval: float = 3600.0) -> None:
        super().__init__(daemon=True, name="housekeeper")
        self.store = store
        self.interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Trigger a sweep now (e.g. after the retention setting changed)."""
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.enforce_once()
            except Exception:
                logger.exception("housekeeper sweep failed")
            # Sleep until the interval elapses or someone wakes/stops us.
            self._wake.wait(self.interval)
            self._wake.clear()

    def enforce_once(self) -> int:
        """Delete media older than retention_days. Returns count removed."""
        cfg = self.store.get()
        days = getattr(cfg, "retention_days", 0)
        if days <= 0:
            return 0
        rec_dir = Path(cfg.recordings_dir)
        if not rec_dir.exists():
            return 0
        cutoff = time.time() - days * 86400
        deleted = 0
        for p in rec_dir.iterdir():
            try:
                if not p.is_file() or p.suffix.lower() not in _MEDIA_EXTS:
                    continue
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except FileNotFoundError:
                continue
            except Exception:
                logger.exception("housekeeper: failed to delete %s", p)
        if deleted:
            logger.info(
                "housekeeper: deleted %d file(s) older than %d day(s)", deleted, days
            )
        return deleted
