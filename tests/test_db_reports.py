import time

from bot.core.config import BotConfig
from bot.data.db import Database
from bot.data.reports import build_report


def test_db_events_sessions_gold(tmp_path):
    db = Database(tmp_path / "t.db")
    db.log_event("test", {"a": 1})
    db.log_event("stuck", {"wp": 2})
    sid = db.start_session("fisher", {"dry_run": True})
    time.sleep(0.02)
    db.end_session(sid, "timeout", {"casts": 5})
    db.add_gold(100)
    db.add_gold(250)

    assert db.event_counts()["test"] == 1
    sess = db.list_sessions()[0]
    assert sess["module"] == "fisher" and sess["status"] == "timeout"
    gold = db.gold_series()
    assert [g["gold"] for g in gold] == [100, 250]

    report = build_report(db, since_hours=1)
    assert "fisher" in report and "150" in report
    db.close()


def test_config_defaults_and_apply(tmp_path):
    cfg = BotConfig()
    assert cfg.fishing.key_cast == "1"
    assert cfg.channel.calibrated is False
    # dataclass asdict roundtrip
    from dataclasses import asdict

    d = asdict(cfg)
    assert d["fishing"]["bite_window_max_s"] == 30.0
