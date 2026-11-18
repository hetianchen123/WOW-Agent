import json
from pathlib import Path

from bot.data.evidence import EvidenceSummary, RunEvidence, build_manifest, replay
from bot.data.db import Database
from bot.modules.context import ModuleContext
from bot.core.config import BotConfig


def test_manifest_hashes_dirty_source_without_claiming_loaded_addon(tmp_path):
    source = tmp_path / "addon/WowAgent"
    source.mkdir(parents=True)
    (source / "WowAgent.toc").write_text("## Version: 0.2.62\n")
    (tmp_path / "bot").mkdir()
    script = tmp_path / "bot/quest.py"
    script.write_text("x = 1")
    first = build_manifest(tmp_path, source)
    script.write_text("x = 2")
    second = build_manifest(tmp_path, source)
    assert first["controller"]["sha256"] != second["controller"]["sha256"]
    assert first["addon_disk_matches_source"]
    assert first["addon_loaded_version"] is None


def test_events_db_and_artifacts_share_ids_and_replay_without_models(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    ctx = ModuleContext(BotConfig(), None, None, None, None, db, None, None, None)
    ctx.evidence = RunEvidence(tmp_path / "runs", {"schema": 1})
    ctx.log_event("quest_action", {"key": "f"})
    ctx.log_event("quest_terrain_request", {"evidence": "terrain/sample/report.json"})
    ctx.log_event("quest_terrain_decision", {"decision": {"startlux_status": "unavailable"}})
    ctx.log_event("quest_suspended", {"id": 90895, "reason": "route_unconfirmed"})
    ctx.log_event("module_end", {"status": "completed_with_pending"})
    report = replay(ctx.evidence.path / "events.jsonl")
    row = json.loads(db.recent_events(limit=1)[0]["data"])
    assert row["run_id"] == ctx.evidence.run_id and row["event_id"] == 5
    assert report["quests_delivered"] == 0
    assert report["last_action"]["data"]["key"] == "f"
    assert report["last_effective_feedback"] is None
    assert report["failures"][0]["category"] == "model_unavailable"
    assert (ctx.evidence.path / "summary.json").is_file()
    db.close()


def test_replay_retained_npc_failure_does_not_invent_visual_evidence():
    report = replay(Path(__file__).parent / "fixtures/npc-blocked-20261115.log")
    assert [item["id"] for item in report["suspended"]] == [90895, 90893]
    assert report["quests_delivered"] == 0
    assert report["last_effective_feedback"] is None
    assert report["skipped_lines"] == 0
    assert {failure["category"] for failure in report["failures"]} == {"route_unconfirmed"}


def test_summary_keeps_inherited_pending_count_without_inventing_a_current_failure():
    summary = EvidenceSummary()
    summary.consume({"kind": "quest_done", "data": {"id": 90893}})
    summary.consume({"kind": "quest_round_summary", "data": {
        "pending": [], "pending_count": 1, "pending_inherited_count": 1, "pending_count_verified": True}})
    result = summary.export()
    assert result["quests_delivered"] == 1 and result["suspended"] == []
    assert result["round_summary"]["pending_count"] == 1
    assert result["round_summary"]["pending_inherited_count"] == 1


def test_broken_evidence_storage_does_not_suppress_existing_database(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    ctx = ModuleContext(BotConfig(), None, None, None, None, db, None, None, None)
    ctx.evidence = RunEvidence(tmp_path / "runs", {})
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(ctx.evidence, "record", fail)
    ctx.log_event("module_end", {"status": "stopped:estop"})
    assert ctx.evidence_failed
    assert db.recent_events()[0]["kind"] == "module_end"
    db.close()


def test_activity_accounting_does_not_assign_unobserved_time_to_movement():
    summary = EvidenceSummary()
    for stamp, activity in ((1, "travel"), (2, "model_wait"), (3, "model_wait"), (30, "combat")):
        summary.consume({"kind": "quest_observation", "monotonic": stamp, "data": {"activity": activity}})
    report = summary.export()
    assert report["sampled_activity_seconds"] == {"travel": 1, "model_wait": 1}
    assert report["unobserved_seconds"] == 27


def test_escape_summary_distinguishes_movement_visibility_route_and_quest_credit():
    summary = EvidenceSummary()
    for kind, data in [
        ('quest_escape_step', {'yards': .5}),
        ('quest_terrain_retreat_step', {'yards': .7}),
        ('quest_terrain_retreat_result', {'yards': 1.2, 'clearance_verified': False}),
        ('quest_escape_observation', {'observation_improved': True}),
        ('quest_terrain_result', {'outcome': 'detoured'}),
        ('quest_terrain_result', {'result': 'detoured'}),  # module wrapper must not double-count
    ]:
        summary.consume({'kind': kind, 'data': data})
    report = summary.export()
    assert report['escape']['steps_with_feedback'] == 2 and report['escape']['moved_yards'] == 1.2
    assert report['escape']['observations_improved'] == report['escape']['routes_verified'] == 1
    assert report['quests_delivered'] == 0 and report['last_effective_feedback'] is None
