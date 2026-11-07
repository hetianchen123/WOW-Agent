"""Integration: real FisherModule against the synthetic game world."""
import pytest

from bot.selftest import run_fisher_session


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
