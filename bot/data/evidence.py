"""Per-run evidence bundles and offline replay, without game input or models."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from ..core.paths import ROOT

FEEDBACK_EVENTS = {
    "quest_accept_confirmed", "quest_done", "quest_object_progress", "quest_resource_progress",
    "quest_combat_credit", "quest_loot_confirmed", "quest_loot_batch_receipt",
    "quest_recovery_complete", "quest_pet_ready", "quest_destination_reached",
}
ACTION_EVENTS = {"quest_action", "quest_combat_action", "quest_defense_action", "quest_loot",
                 "quest_fetch", "quest_turn_in_approach", "quest_approach_issued", "quest_model_movement_issued"}


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def fingerprint(directory: Path, suffixes: tuple[str, ...]) -> dict:
    files = {}
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                files[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"sha256": digest, "files": files}


def build_manifest(root: Path = ROOT, installed: Path | None = None) -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    source = fingerprint(root / "addon/WowAgent", (".lua", ".toc"))
    disk = fingerprint(installed, (".lua", ".toc")) if installed else None
    toc = root / "addon/WowAgent/WowAgent.toc"
    version = next((line.partition(":")[2].strip() for line in toc.read_text().splitlines()
                    if line.startswith("## Version:")), None) if toc.exists() else None
    return {"schema": 1, "git_commit": commit,
            "controller": fingerprint(root / "bot", (".py",)),
            "addon_source": {"version": version, **source}, "addon_installed": disk,
            "addon_disk_matches_source": bool(disk and disk["files"] and disk == source),
            "addon_loaded_version": None,  # Disk equality does not prove /reload.
            "models": {"startlux": fingerprint(root / "data/startlux/model", (".json",)),
                       "qwen": fingerprint(root / "data/qwen3-vl/model", (".json",)),
                       "qwen_runner": fingerprint(root / "experiments/qwen3_vl", (".py",))}}


class EvidenceSummary:
    def __init__(self):
        self.counts = Counter()
        self.suspended = []
        self.round_summary = None
        self.last_action = self.last_feedback = self.last_observation = None
        self.failures = []
        self.runtime = {}
        self.progress = {}
        self.completed = set()
        self.last_exit = None
        self.activity_seconds = Counter()
        self.unobserved_seconds = 0.0
        self.escape = {"steps_with_feedback": 0, "moved_yards": 0.0, "observations_improved": 0,
                       "routes_verified": 0, "last_result": None}

    def consume(self, record):
        kind, data = record["kind"], record.get("data") or {}
        self.counts[kind] += 1
        if kind in {"quest_escape_step", "quest_terrain_retreat_step"}:
            self.escape["steps_with_feedback"] += 1
            self.escape["moved_yards"] += max(0, data.get("yards", 0))
        elif kind == "quest_escape_observation" and data.get("observation_improved") is True:
            self.escape["observations_improved"] += 1
        elif kind == "quest_terrain_retreat_result":
            self.escape["last_result"] = record
        elif kind == "quest_terrain_result" and data.get("outcome") == "detoured":
            self.escape["routes_verified"] += 1
        if kind in ACTION_EVENTS:
            self.last_action = record
        effective = kind in FEEDBACK_EVENTS
        if kind in {"quest_object_progress", "quest_resource_progress"}:
            key = (kind, data.get("id"))
            value = data.get("progress", data.get("percent"))
            effective = key in self.progress and self.progress[key] != value
            self.progress[key] = value
        if effective:
            self.last_feedback = record
        if kind == "quest_done":
            self.completed.add(data.get("id"))
        if kind in {"module_end", "quest_stop_requested"}:
            self.last_exit = record
        if kind == "quest_observation":
            if self.last_observation and "monotonic" in record and "monotonic" in self.last_observation:
                elapsed = record["monotonic"] - self.last_observation["monotonic"]
                if 0 <= elapsed <= 2.5:
                    self.activity_seconds[self.last_observation["data"].get("activity", "unclassified_wait")] += elapsed
                elif elapsed > 2.5:
                    self.unobserved_seconds += elapsed
            self.last_observation = record
        if kind in {"quest_navigation_start", "quest_combat_profile", "quest_addon_loaded"}:
            self.runtime[kind] = data
        if kind == "quest_suspended":
            self.suspended.append(data)
        if kind == "quest_round_summary":
            self.round_summary = data
        category = None
        decision = data.get("decision") or {}
        if (data.get("startlux_status") == "unavailable" or decision.get("startlux_status") == "unavailable"
                or kind == "quest_npc_visual_unavailable"):
            category = "model_unavailable"
        elif kind == "quest_terrain_candidate_rejected":
            category = "candidate_rejected"
        elif kind in {"quest_turn_in_approach_result", "quest_approach_result"} and (
                data.get("reason") == "no_displacement" or data.get("moved_yards", 1) == 0):
            category = "no_displacement"
        elif kind in {"module_crash", "input_session_stopped"}:
            category = kind
        elif kind.endswith("_error"):
            category = "observation_or_decision_error"
        elif kind == "quest_npc_visual_result" and data.get("result") != "detoured":
            category = "route_unconfirmed"
        elif kind == "quest_stop_requested":
            category = "stop_or_suspend_requested"
        if category:
            self.failures.append({"category": category, "event": record})

    def export(self):
        return {"event_counts": dict(self.counts), "suspended": self.suspended,
                "round_summary": self.round_summary,
                "quests_delivered": len(self.completed),
                "escape": {**self.escape, "moved_yards": round(self.escape["moved_yards"], 2)},
                "last_exit": self.last_exit,
                "sampled_activity_seconds": {key: round(value, 2) for key, value in self.activity_seconds.items()},
                "unobserved_seconds": round(self.unobserved_seconds, 2),
                "last_action": self.last_action, "last_effective_feedback": self.last_feedback,
                "last_observation": self.last_observation, "failures": self.failures,
                "runtime": self.runtime}


class RunEvidence:
    def __init__(self, directory: Path, manifest: dict):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.path = directory / self.run_id
        self.path.mkdir(parents=True)
        self.summary = EvidenceSummary()
        self.lock = threading.RLock()
        self.ordinal = 0
        self._write("manifest.json", {"run_id": self.run_id, **manifest})

    def _write(self, name, value):
        temporary = self.path / (name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=json_default))
        temporary.replace(self.path / name)

    def record(self, kind, data, state=None):
        with self.lock:
            self.ordinal += 1
            record = {"run_id": self.run_id, "event_id": self.ordinal, "ts": time.time(),
                      "monotonic": time.monotonic(), "state": state, "kind": kind, "data": data or {}}
            with (self.path / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")
            self.summary.consume(record)
            if kind in {"module_end", "quest_suspended", "module_crash", "quest_round_summary"}:
                self._write("summary.json", self.summary.export())
            return {**(data or {}), "run_id": self.run_id, "event_id": self.ordinal}


def replay(path: Path) -> dict:
    """Replay structured events or retained Python INFO logs, never guessing missing evidence."""
    summary = EvidenceSummary()
    skipped = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            if line.startswith("{"):
                record = json.loads(line)
            elif " INFO    module: " in line:
                prefix, _, event = line.partition(" INFO    module: ")
                kind, _, payload = event.partition(" ")
                record = {"ts": prefix, "kind": kind, "data": ast.literal_eval(payload) if payload else {}}
            else:
                continue
            summary.consume(record)
        except (ValueError, SyntaxError, KeyError, TypeError):
            skipped += 1
    return {"source": str(path), "mode": "offline_event_replay", "skipped_lines": skipped, **summary.export()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.log), ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
