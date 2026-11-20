"""Call the installed local StartLux service without blocking the control loop."""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
from threading import Event, Lock
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..core.paths import DATA_DIR

REQUEST_TIMEOUT_S = 8.0
DECISION_WAIT_S = 2 * REQUEST_TIMEOUT_S + 2
OBSERVATION_MAX_AGE_MS = 2500
log = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalStartLux:
    def __init__(self, port: int = 8090, timeout: float = REQUEST_TIMEOUT_S, *, autostart=False):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("StartLux 本地端口无效")
        self.url = f"http://127.0.0.1:{port}/v1/systemone"
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.port, self.autostart = port, autostart
        self.server = None
        self.closed, self.service_lock = Event(), Lock()
        self.start_failed = False

    def service_ready(self):
        try:
            with self.opener.open(f"http://127.0.0.1:{self.port}/health", timeout=.5) as response:
                health = json.load(response)
        except URLError:
            return False
        if not str(health.get("model", "")).startswith("StartLux-Decision"):
            raise ValueError("本地端口的服务不是 StartLux")
        return health.get("status") == "ok"

    def ensure_service(self):
        """Called in the decision worker only; ordinary navigation never waits."""
        if self.closed.is_set():
            raise ValueError("StartLux client closed")
        if self.service_ready():
            return
        with self.service_lock:
            if self.closed.is_set() or self.start_failed:
                raise ValueError("StartLux startup unavailable for this session")
            if self.server is None:
                executable = DATA_DIR / "startlux/.venv/bin/startlux-decide"
                if not executable.is_file():
                    raise ValueError("StartLux executable missing")
                with (DATA_DIR / "startlux/server.log").open("a") as output:
                    self.server = subprocess.Popen([
                        str(executable), "serve", "--backend", "mlx", "--model", str(DATA_DIR / "startlux/model"),
                        "--name", "StartLux-Decision-v1.1-9B-Instant-MLX-4bit",
                        "--host", "127.0.0.1", "--port", str(self.port),
                    ], stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                       env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                            "HF_HUB_DISABLE_TELEMETRY": "1"})
                log.info("startlux_service_starting port=%s pid=%s", self.port, self.server.pid)
        deadline = time.monotonic() + 15
        while not self.closed.is_set():
            if self.service_ready():
                log.info("startlux_service_ready port=%s", self.port)
                return
            server = self.server
            if server is None:
                raise ValueError("StartLux startup cancelled")
            if server.poll() is not None or time.monotonic() >= deadline:
                self.start_failed = True
                self.stop_owned_service()
                raise RuntimeError("StartLux 未在 15 秒内就绪，详见 data/startlux/server.log")
            self.closed.wait(.25)
        raise ValueError("StartLux startup cancelled")

    def stop_owned_service(self):
        with self.service_lock:
            server, self.server = self.server, None
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)

    def close(self):
        self.closed.set()
        self.stop_owned_service()

    def decide(self, state: dict, criteria: dict[str, str], *, instructions: str | None = None) -> dict:
        if state.get("observation_age_ms", 0) > OBSERVATION_MAX_AGE_MS:
            return {"recommendation": None, "reason": "stale_state"}
        if self.autostart:
            self.ensure_service()
        request = Request(self.url, data=json.dumps({
            "state": state,
            "questions": {"action": {
                "type": "choice",
                "instructions": instructions or (
                    "选择下一段任务操作。任务未接取且允许接取时接任务，已接取时完成目标，"
                    "目标已完成时交付。有不匹配的对话、战斗或未知状态时等待。"
                ),
                "criteria": criteria,
            }},
            "none_check": True,
            "allow_none": False,
        }, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise
        answer = result["answers"]["action"]
        choice, probabilities = answer["choice"], answer["probabilities"]
        confidence, none_probability = answer["confidence"], answer["none_probability"]
        if choice not in criteria or probabilities.keys() != criteria.keys():
            raise ValueError("StartLux 返回了候选范围外的动作")
        values = (confidence, none_probability, *probabilities.values())
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 <= value <= 1 for value in values):
            raise ValueError("StartLux 返回了无效概率")
        model = result["model"]
        if not isinstance(model, str) or not model.startswith("StartLux-Decision"):
            raise ValueError("本地服务返回的模型并非 StartLux-Decision")
        reason = None
        if none_probability >= .20:
            reason = "no_option_fits"
        elif confidence < .55:
            reason = "low_confidence"
        return {
            "model": model, "choice": choice, "confidence": confidence,
            "none_probability": none_probability, "probabilities": probabilities,
            "recommendation": choice if reason is None else None, "reason": reason,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


class QuestPlanner:
    """One request in flight, with phase identity and expiry checked on return."""

    def __init__(self, client: LocalStartLux):
        self.client = client
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="startlux")
        self.future: Future | None = None
        self.signature = None
        self.started = 0.0
        self.retried = False

    def poll(self, signature: tuple, state: dict, criteria: dict[str, str], *,
             instructions: str | None = None, max_age_s: float = REQUEST_TIMEOUT_S + 1) -> dict | None:
        if self.future is None:
            if signature != self.signature:
                self.retried = False
            self.signature = signature
            self.started = time.monotonic()
            options = {"instructions": instructions} if instructions is not None else {}
            self.future = self.pool.submit(self.client.decide, state, criteria, **options)
            return None
        if not self.future.done():
            return None
        future, requested = self.future, self.signature
        self.future = None
        if signature != requested:
            return None
        try:
            result = future.result()
        except TimeoutError:
            if self.retried:
                raise
            self.retried = True
            log.warning("StartLux 首次推理超时，将用当前任务状态重试一次。")
            return None
        if (time.monotonic() - self.started > max_age_s
                or state.get("observation_age_ms", 0) > OBSERVATION_MAX_AGE_MS):
            return {**result, "recommendation": None, "reason": "stale_decision"}
        return result

    def discard_pending(self):
        if self.future is not None and self.future.cancel():
            self.future = None
        self.signature = None
        self.retried = False

    def close(self):
        if self.future:
            self.future.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if hasattr(self.client, "close"):
            self.client.close()
