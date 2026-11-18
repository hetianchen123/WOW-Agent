"""Owned, lazy, single-flight MLX worker. No socket or external service required."""
import atexit
import json
import os
import selectors
import subprocess
import threading
import time
import uuid

from ..core.paths import DATA_DIR, ROOT


class VisionWorker:
    def __init__(self, command=None, stderr_path=None):
        self.command = command or [str(DATA_DIR / "startlux/.venv/bin/python"),
                                   str(ROOT / "experiments/qwen3_vl/run.py"), "--worker"]
        self.stderr_path = stderr_path or DATA_DIR / "qwen3-vl/worker.log"
        self.process = None
        self.buffer = b""
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.load_s = None

    def _stop(self):
        process, self.process = self.process, None
        self.buffer = b""
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()

    def close(self):
        self.closed.set()
        # Active readers notice cancellation within .1 s and own process cleanup.
        if self.lock.acquire(timeout=.2):
            try:
                self._stop()
            finally:
                self.lock.release()

    def _check(self, guard):
        if self.closed.is_set():
            raise ValueError("vision worker closed")
        if guard:
            guard()

    def _read(self, deadline, guard):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                self._check(guard)
                if time.monotonic() >= deadline:
                    raise ValueError("Qwen 视觉推理超时")
                if b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    return json.loads(line)
                if selector.select(timeout=.1):
                    block = os.read(self.process.stdout.fileno(), 65536)
                    if not block:
                        raise ValueError("Qwen worker exited; see " + str(self.stderr_path))
                    self.buffer += block
                    if len(self.buffer) > 2_000_000:
                        raise ValueError("oversized vision response")

    def inspect(self, images, *, crop=None, target=None, nearfield=False, composition=False, guard=None, timeout=40,
                player_identity=None):
        deadline = time.monotonic() + timeout
        while not self.lock.acquire(timeout=.1):
            self._check(guard)
            if time.monotonic() >= deadline:
                raise ValueError("Qwen worker queue timeout")
        try:
            self._check(guard)
            if self.process is None:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                with self.stderr_path.open("ab") as errors:
                    self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=errors, bufsize=0)
                ready = self._read(deadline, guard)
                if ready.get("protocol") != 1 or ready.get("ready") is not True:
                    raise ValueError("invalid vision worker handshake")
                self.load_s = ready.get("load_s")
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "images": [str(path) for path in images],
                       "crop": crop, "target": target, "nearfield": nearfield, "composition": composition,
                       "player_identity": player_identity}
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            self.process.stdin.flush()
            result = self._read(deadline, guard)
            if result.get("id") != request_id or result.get("error"):
                raise ValueError(result.get("error", "vision response belongs to another request"))
            self._check(guard)
            return result["cases"]
        except BaseException:
            # Never leave an old inference to be consumed by the next scene.
            self._stop()
            raise
        finally:
            self.lock.release()


_worker = None
_worker_lock = threading.Lock()


def get_worker():
    global _worker
    with _worker_lock:
        if _worker is None or _worker.closed.is_set():
            _worker = VisionWorker()
        return _worker


def close_worker():
    with _worker_lock:
        if _worker:
            _worker.close()


atexit.register(close_worker)
