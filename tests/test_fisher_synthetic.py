"""Integration: real FisherModule against the synthetic game world."""
import pytest

from bot.selftest import run_fisher_session


def test_timeout_drains_final_input_without_confirming_future_results(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bot import selftest

    ctx, inp, game, db = selftest.build_selftest_context(db_path=str(tmp_path / "selftest.db"))
    monkeypatch.setattr(selftest, "build_selftest_context", lambda: (ctx, inp, game, db))

    def final_tick(module, duration):
        inp.down("1")
        inp.up("1")
        assert game._cast_count == 0  # No screen capture followed this input.
        return SimpleNamespace(status="timeout", stats={"casts": 1, "loots": 0})

    monkeypatch.setattr(selftest.FisherModule, "run", final_tick)
    stats = run_fisher_session(0.01)
    assert stats["world_casts"] == stats["casts"] == 1
    assert stats["world_loots"] == 0 and game.tracked_total == 0
    assert game.pending and not game.ev_queue  # Results remain unconfirmed.


@pytest.mark.slow
def test_fisher_synthetic_session():
    stats = run_fisher_session(max_duration_s=10.0)
    print(stats)
    assert not stats["outcome"].startswith("stopped"), stats
    assert stats["casts"] >= 2, stats
    assert stats["loots"] >= 1, stats
    assert stats["bites"] >= 1, stats
    # world saw every cast; loots may trail by a cycle cut off at timeout
    assert stats["world_casts"] == stats["casts"]
    assert stats["world_loots"] >= stats["loots"]
    assert stats["channel_bad"] <= stats["channel_ok"] * 0.05 + 3
