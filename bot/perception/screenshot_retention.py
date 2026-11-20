"""Bound automatic image evidence without removing reports or saved fixtures."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.paths import DATA_DIR, SCREENSHOT_DIR
from .cv import save_snapshot

log = logging.getLogger(__name__)
COMPLETE = ".wowagent-captures-complete"
SNAPSHOT_NAME = re.compile(r"\d{8}_\d{6}_[\w-]+\.png")


def mark_captures_complete(folder: Path) -> None:
    try:
        (folder / COMPLETE).touch()
    except OSError:
        log.debug("could not mark completed screenshot batch: %s", folder)


def prune_screenshots(keep_hours: int, max_mib: int, *, screenshots=SCREENSHOT_DIR,
                      terrain=DATA_DIR / "terrain", now=None) -> tuple[int, int]:
    """Apply age and byte limits to the automatic screenshot cache."""
    now = time.time() if now is None else now
    expires = now - max(0, keep_hours) * 3600
    limit = max(0, max_mib) * 1024**2
    paths = [p for p in screenshots.glob("*.png") if SNAPSHOT_NAME.fullmatch(p.name)]
    for folder in terrain.glob("*/" + COMPLETE):
        if not folder.parent.is_symlink():
            paths.extend(folder.parent.glob("*.png"))
    images = []
    for path in paths:
        try:
            if not path.is_symlink() and path.is_file():
                stat = path.stat()
                images.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    remaining = sum(size for _, size, _ in images)
    removed, freed = 0, 0
    for modified, size, path in sorted(images):
        if modified >= expires and remaining <= limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
        remaining -= size
    return removed, freed


class ScreenshotWriter:
    """One diagnostic frame in flight. Control never waits for image encoding."""

    def __init__(self, keep_hours: int, max_mib: int):
        self.keep_hours, self.max_mib = keep_hours, max_mib
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screenshot")
        self.job = None
        self.next_prune = 0.

    @property
    def available(self):
        return self.job is None or self.job.done()

    def prune(self, now):
        if self.available and now >= self.next_prune:
            self.next_prune = now + 300
            self.job = self.pool.submit(self.prune_now)

    def prune_now(self):
        removed, freed = prune_screenshots(self.keep_hours, self.max_mib)
        if removed:
            log.info("expired diagnostic screenshots: %s files, %.1f MiB", removed, freed / 1024**2)

    def submit(self, image, tag):
        if not self.available or self.keep_hours <= 0 or self.max_mib <= 0:
            return None
        path = SCREENSHOT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png"
        self.job = self.pool.submit(save_snapshot, image, tag, path=path)
        return path

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
