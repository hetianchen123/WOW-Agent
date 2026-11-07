"""Call the installed local StartLux service without blocking the control loop."""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalStartLux:
    def __init__(self, port: int = 8090, timeout: float = 3.0):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("StartLux 本地端口无效")
        self.url = f"http://127.0.0.1:{port}/v1/systemone"
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def decide(self, state: dict, criteria: dict[str, str]) -> dict:
        if state.get("observation_age_ms", 0) > 2500:
            return {"recommendation": None, "reason": "stale_state"}
        request = Request(self.url, data=json.dumps({
            "state": state,
            "questions": {"action": {
                "type": "choice",
                "instructions": (
                    "选择下一段任务操作。任务未接取且允许接取时接任务，已接取时完成目标，"
                    "目标已完成时交付。有不匹配的对话、战斗或未知状态时等待。"
                ),
                "criteria": criteria,
            }},
            "none_check": True,
            "allow_none": False,
        }, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
        started = time.monotonic()
        with self.opener.open(request, timeout=self.timeout) as response:
            result = json.load(response)
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

    def poll(self, signature: tuple, state: dict, criteria: dict[str, str]) -> dict | None:
        if self.future is None:
            self.signature = signature
            self.started = time.monotonic() - state.get("observation_age_ms", 0) / 1000
            self.future = self.pool.submit(self.client.decide, state, criteria)
            return None
        if not self.future.done():
            return None
        future, requested = self.future, self.signature
        self.future = None
        if signature != requested:
            return None
        result = future.result()
        if time.monotonic() - self.started > 2.5:
            return {**result, "recommendation": None, "reason": "stale_decision"}
        return result

    def close(self):
        if self.future:
            self.future.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)
