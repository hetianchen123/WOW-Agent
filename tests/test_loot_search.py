from types import SimpleNamespace

import pytest

from bot.control.input_mock import MockInput
from bot.control.quest_search import QuestAreaSearch
from bot.modules.loot import LootSweep
from bot.perception.travel_channel import TravelFlags


def test_loot_batch_repeats_after_real_receipts_and_stops_without_feedback():
    events = []
    ctx = SimpleNamespace(input=MockInput(), log_event=lambda *args: events.append(args))
    nav = SimpleNamespace(sweep_ready=True, fetch_ready=True, loot_epoch=254, flags=TravelFlags(0))
    sweep = LootSweep(0)
    assert sweep.tick(ctx, 0, nav)
    assert ctx.input.count("key_down", "o") == 1
    # Pet busy: do not abort the batch or resend before feedback.
    nav.fetch_ready = False
    assert sweep.tick(ctx, 2, nav)
    nav.loot_epoch = 255
    assert sweep.tick(ctx, 3, nav)
    nav.fetch_ready = True
    assert sweep.tick(ctx, 4.6, nav)
    assert ctx.input.count("key_down", "o") == 2
    nav.loot_epoch = 0  # Wrapped receipt counter is still a new result.
    assert sweep.tick(ctx, 6, nav)
    assert sweep.tick(ctx, 7.6, nav)
    assert ctx.input.count("key_down", "o") == 3
    assert not sweep.tick(ctx, 16, nav)
    assert not sweep.tick(ctx, 30, nav)
    assert events[-1][1]["all_corpses_cleared"] == "unknown"


def test_loot_batch_never_sends_unverified_or_in_combat_input():
    ctx = SimpleNamespace(input=MockInput(), log_event=lambda *_: None)
    nav = SimpleNamespace(sweep_ready=True, fetch_ready=True, loot_epoch=0, flags=TravelFlags.IN_COMBAT)
    sweep = LootSweep(0)
    assert sweep.tick(ctx, 0, nav) and not ctx.input.count("key_down")
    nav.flags, nav.sweep_ready = TravelFlags(0), False
    assert not sweep.tick(ctx, 1, nav) and not ctx.input.count("key_down")


def test_native_patrol_uses_unvisited_blob_points_instead_of_returning_to_center():
    points = ((.53, .5), (.56, .5), (.59, .5), (.53, .53))
    nav = SimpleNamespace(quest_id=1, map_id=2, world_size=(1000, 1000), destination=(.5, .5), search_area=points)
    search = QuestAreaSearch(nav)
    position = (.55, .5)
    selected = []
    while (point := search.next(position)) is not None:
        assert point in points and point not in selected and point != nav.destination
        selected.append(point)
        position = point
    assert (.59, .5) in selected
    assert search.native


def test_fallback_patrol_expands_beyond_old_22_yards_without_unbounded_search():
    nav = SimpleNamespace(quest_id=1, map_id=2, world_size=(1000, 1000), destination=(.5, .5), search_area=None)
    search = QuestAreaSearch(nav)
    position, distances = (.5, .5), []
    while (point := search.next(position)) is not None:
        distances.append(search.router.distance(nav.destination, point))
        position = point
    assert max(distances) == pytest.approx(90)
    assert len(distances) <= 32 and not search.native
