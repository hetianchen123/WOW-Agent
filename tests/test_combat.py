import json
import math
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bot.combat_cli import ability, class_catalog, rotation, saved_profile, spell_macro
from bot.data.combat import EDITIONS, build_catalog, open_catalog
from bot.control.input_mock import MockInput
from bot.modules import combat
from bot.modules.combat import Fight, QuestHunt
from bot.perception import channel_spec as wire
from bot.perception.combat_channel import CombatSample, decode_combat
from bot.perception.quest_channel import QuestState
from bot.perception.travel_channel import TaskFlags, TravelFlags, TravelSample


def test_forever_hunter_can_change_verified_output_at_melee_range():
    ranged = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 4, 8, 1,
                         in_range=True)
    fight = Fight(6, immediate=True)
    assert fight.step(0, ranged) == ("action", "4")
    melee = replace(ranged, key=5, sequence=2, level=2)
    assert fight.step(1, melee) == ("action", "5")
    assert fight.step(2, replace(melee, key=6, sequence=3))[0] == "action_binding_changed"


@pytest.mark.parametrize('prepared', [False, True])
def test_live_fight_requires_loot_to_attack_and_waits_for_corpse_receipt(monkeypatch, prepared):
    now = [0.]
    monkeypatch.setattr(combat, 'time', SimpleNamespace(monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    alive = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 4, 8, 1, in_range=True)
    corpse = replace(alive, target_dead=True)
    nav = TravelSample(47, 1429, 1429, None, None, None, 0, QuestState.ACTIVE, TravelFlags(0), 1,
        task=TaskFlags.PREPARED | TaskFlags.LOOT_IN_RANGE, addon_build=323 if prepared else 322,
        corpse_lootable=True, loot_epoch=3)
    frames = iter([(alive, nav), (replace(corpse, sequence=2), replace(nav, sequence=2)),
        (replace(corpse, sequence=3), replace(nav, sequence=3)),
        (replace(corpse, sequence=4), replace(nav, sequence=4, corpse_lootable=False, loot_epoch=4))])
    keys = MockInput()
    result = combat.run_fight(Fight(6, immediate=True), None, keys, lambda: False, lambda: True, 10,
                              read_with_loot=lambda: next(frames))
    assert result == ('corpse_cleared' if prepared else 'loot_addon_required')
    assert keys.count('key_down', 'f') == keys.count('key_down', '4') == int(prepared)


def test_forever_hunt_resumes_after_verified_dead_target_without_credit():
    sample = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 4, 8, 1,
                          in_range=True)
    hunt = QuestHunt(frozenset({6}))
    assert hunt.step(0, replace(sample, tap_denied=True), 2, False) == ("select_target", "tab")
    assert hunt.fight is None
    assert hunt.step(1, replace(sample, sequence=2), 2, False)[0] == "waiting"
    assert hunt.step(2, replace(sample, sequence=3), 2, False) == ("action", "4")
    corpse = replace(sample, sequence=4, target_dead=True)
    assert hunt.step(3, corpse, 2, False)[0] == "waiting"
    assert hunt.step(8, replace(corpse, sequence=5), 2, False)[0] == "uncredited_target"
    assert hunt.fight is None and hunt.uncredited_kills == 1


def test_forever_hunt_drops_target_tagged_before_first_hit():
    sample = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 4, 8, 1,
                          in_range=True)
    hunt = QuestHunt(frozenset({6}))
    assert hunt.step(0, sample, 2, False) == ("waiting", None)
    assert hunt.step(.2, replace(sample, sequence=2, tap_denied=True), 2, False) == (
        "target_tagged_by_other_player", None)
    assert hunt.fight is None
    assert hunt.step(.4, replace(sample, sequence=3, tap_denied=True), 2, False) == ("select_target", "tab")


def test_forever_melee_waits_for_next_swing_receipt():
    sample = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 5, 8, 1,
                          in_range=True, cast_epoch=0)
    fight = Fight(6, immediate=True)
    assert fight.step(0, sample) == ("action", "5")
    assert fight.step(2, replace(sample, sequence=2)) == ("waiting", None)
    assert fight.presses == 1
    assert fight.step(3, replace(sample, sequence=3, cast_epoch=1)) == ("action", "5")
    assert fight.no_receipts == 0


def test_forever_keeps_rejected_target_out_of_next_search_until_selection_changes():
    sample = CombatSample(3, 1, 18, 70959, 1485, 1, 0, 6, False, True, False, 5, 8, 1,
                          in_range=True)
    hunt = QuestHunt(frozenset({6}))
    hunt.rejected_selection = (6, 8)
    assert hunt.step(0, sample, 6, False) == ("select_target", "tab")
    assert hunt.fight is None
    assert hunt.step(1, replace(sample, sequence=2, state_epoch=9), 6, False) == ("waiting", None)
    assert hunt.fight is not None and hunt.rejected_selection is None


def test_combat_rows_validate_before_using_any_state():
    rows = [8 + 1 * 32 + 1 * 8192, 70783, 63 + 80 * 4096, 87, 12345,
            2 + 5 * 8 + 8 * 128 + 100 * 8192]
    img = np.zeros((6 * wire.REGION_H, wire.REGION_W, 3), dtype=np.uint8)
    for index, value in enumerate(rows):
        wire.render_frame(img, 0, index * wire.REGION_H, wire.encode_frame(10 + index, value))
    assert decode_combat(img) == CombatSample(
        8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 100,
    )
    img[3 * wire.REGION_H:4 * wire.REGION_H] = 0
    assert decode_combat(img) is None


def test_fight_requires_fresh_telemetry_and_observes_death():
    fight = Fight(12345)
    sample = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.10, sample) == ("waiting", None)
    assert fight.step(.20, replace(sample, sequence=2)) == ("action", "5")
    assert fight.step(.40, replace(sample, sequence=3)) == ("waiting", None)
    assert fight.step(.50, replace(sample, sequence=4, target_dead=True)) == ("target_defeated", None)
    assert fight.presses == 1


@pytest.mark.parametrize("receipts, expected, presses", [
    (False, "no_cast_or_damage_receipt", 3),
    (True, "no_progress", 12),
])
def test_restricted_health_keeps_cast_receipt_separate_from_damage(receipts, expected, presses):
    fight = Fight(244669, approach=True)
    sample = CombatSample(3, 1, 1, 70783, 253, 80, 87, 244669, False, True, False, 1, 8, 1,
                          in_range=True, target_health=None, cast_epoch=127)
    assert fight.step(0, sample) == ("waiting", None)
    sent = 0
    for index in range(1, 17):
        epoch = (127 + sent) % 128 if receipts else 127
        status, key = fight.step(index, replace(sample, cast_epoch=epoch, sequence=index + 1))
        if key:
            assert (status, key) == ("action", "1")
            sent += 1
        elif status == expected:
            break
    assert status == expected and sent == presses


def test_forever_hidden_health_with_cast_receipts_keeps_quest_fight_bounded():
    hunt = QuestHunt(frozenset({257}))
    sample = CombatSample(3, 1, 18, 70959, 1485, 2, 0, 257, False, True, False, 5, 8, 1,
                          in_range=True, target_engaged=True, cast_epoch=0)
    casts = 0
    for tick in range(60):
        status, key = hunt.step(tick, replace(sample, sequence=tick + 1, cast_epoch=casts),
                                0, in_combat=True)
        assert status in {"waiting", "action"}
        if key:
            casts += 1
    assert hunt.step(60, replace(sample, sequence=61, cast_epoch=casts), 0,
                     in_combat=True) == ("fight_timeout", None)


def test_melee_closes_only_while_out_of_range():
    fight = Fight(244669, approach=True)
    sample = CombatSample(3, 1, 1, 70783, 255, 80, 87, 244669, False, True, False, 1, 8, 1,
                          in_range=False, cast_epoch=0)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.3, replace(sample, sequence=2)) == ("approach", "w")
    assert fight.step(.6, replace(sample, sequence=3, in_range=True)) == ("action", "1")
    assert fight.step(.9, replace(sample, sequence=4, state_epoch=9)) == ("target_or_arm_changed", None)


def test_facing_recovery_turns_on_new_error_and_retries_same_target():
    fight = Fight(12345)
    sample = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1,
                          facing=0, facing_error=0, turn_verified=True)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.2, replace(sample, sequence=2)) == ("action", "5")
    error = replace(sample, sequence=3, facing_error=1)
    assert fight.step(.4, error) == ("turn", "left")
    assert fight.step(.5, error) == ("waiting", None)
    turned = replace(error, facing=math.radians(24))
    assert fight.step(.7, replace(turned, sequence=4)) == ("waiting", None)
    assert fight.step(1.1, replace(turned, sequence=5)) == ("action", "5")
    assert fight.step(1.7, replace(turned, sequence=6, facing_error=2)) == ("facing_unconfirmed", None)
    assert fight.step(1.9, replace(turned, sequence=7, state_epoch=9)) == ("target_or_arm_changed", None)
    assert fight.presses == 2


def test_forever_engaged_target_allows_another_verified_turn():
    fight = Fight(80)
    sample = CombatSample(3, 1, 18, 70959, 1485, 3, 0, 80, False, True, False, 5, 8, 1,
                          target_engaged=True, in_range=True, cast_epoch=0, facing=0,
                          facing_error=0, turn_verified=True)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.2, replace(sample, sequence=2)) == ("action", "5")
    assert fight.step(.4, replace(sample, sequence=3, facing_error=1)) == ("turn", "left")
    turned = replace(sample, sequence=4, facing_error=1, facing=math.radians(48))
    assert fight.step(1.1, turned) == ("action", "5")
    assert fight.step(1.7, replace(turned, sequence=5, facing_error=2)) == ("turn", "left")
    assert fight.last_facing_at == pytest.approx(1.1)


def test_facing_recovery_does_not_turn_with_unverified_keys():
    fight = Fight(12345)
    sample = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1,
                          facing=0, facing_error=0)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.2, replace(sample, sequence=2)) == ("action", "5")
    assert fight.step(.4, replace(sample, sequence=3, facing_error=1)) == ("facing_feedback_unavailable", None)


def test_hunt_sweeps_before_retabbing_and_locks_target_until_in_range():
    hunt = QuestHunt(frozenset({12345}))
    base = CombatSample(8, 1, 1, 70783, 63, 80, 87, 0, False, True, False, 5, 8, 1,
                        facing=0, turn_verified=True)
    assert hunt.step(0, base, 0, False) == ("select_target", "tab")
    assert hunt.step(1.1, replace(base, sequence=2), 0, False) == ("search_turn", "left")
    sector = replace(base, sequence=3, facing=math.radians(20))
    assert hunt.step(1.25, sector, 0, False) == ("waiting", None)
    assert hunt.step(1.45, replace(sector, sequence=4), 0, False) == ("waiting", None)
    assert hunt.step(1.65, replace(sector, sequence=5), 0, False) == ("select_target", "tab")
    target = replace(sector, sequence=6, target_id=12345, in_range=False)
    assert hunt.step(1.7, target, 0, False) == ("waiting", None)
    assert hunt.step(1.9, replace(target, sequence=7), 0, False) == ("approach", "w")
    assert hunt.step(2.3, replace(target, sequence=8), 0, False) == ("waiting", None)
    assert hunt.fight.presses == 0
    assert hunt.step(2.6, replace(target, sequence=9, in_range=True), 0, False) == ("action", "5")


def test_search_does_not_reverse_rapidly_on_overshoot():
    hunt = QuestHunt(frozenset({12345}))
    base = CombatSample(8, 1, 1, 70783, 63, 80, 87, 0, False, True, False, 5, 8, 1,
                        facing=0, turn_verified=True)
    assert hunt.step(0, base, 0, False) == ("select_target", "tab")
    assert hunt.step(1.1, replace(base, sequence=2), 0, False) == ("search_turn", "left")
    overshoot = replace(base, facing=math.radians(32))
    for seq, now in enumerate((1.2, 1.3, 1.4), 3):
        assert hunt.step(now, replace(overshoot, sequence=seq), 0, False) == ("waiting", None)
    assert hunt.step(1.6, replace(overshoot, sequence=6), 0, False) == ("search_turn", "right")


@pytest.mark.parametrize(("change", "expected"), [
    ({"target_id": 0}, "target_missing_or_changed"),
    ({"state_epoch": 9}, "target_or_arm_changed"),
    ({"spec_id": 64}, "character_profile_changed"),
    ({"key_verified": False}, "unverified_action_binding"),
    ({"key": 6}, "action_binding_changed"),
    ({"unsafe": True}, "unsafe_player_state"),
])
def test_fight_stops_when_live_state_changes(change, expected):
    fight = Fight(12345)
    sample = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.20, replace(sample, sequence=2, **change)) == (expected, None)
    assert fight.presses == 0


def test_frozen_frame_and_missing_frame_stop_fight():
    fight = Fight(12345)
    sample = CombatSample(8, 1, 1, 70783, 63, 80, 87, 12345, False, True, False, 5, 8, 1)
    assert fight.step(0, sample) == ("waiting", None)
    assert fight.step(.90, sample) == ("telemetry_stale", None)
    assert fight.step(1.0, None) == ("telemetry_missing", None)


def test_catalog_keeps_client_data_and_reference_separate(tmp_path):
    for edition in EDITIONS:
        (tmp_path / f"{edition}-SpellName.csv").write_text(
            f"ID,Name_lang\n133,{edition} Fireball\n", encoding="utf-8",
        )
    (tmp_path / "retail-SpellEffect.csv").write_text("ID,SpellID\n1234,133\n", encoding="utf-8")
    (tmp_path / "retail-ChrClasses.csv").write_text("ID,Name_lang\n8,Mage\n", encoding="utf-8")
    (tmp_path / "retail-SkillLineAbility.csv").write_text(
        "ID,ClassMask,Spell\n1,128,133\n2,1,133\n", encoding="utf-8",
    )
    (tmp_path / "retail-AssistedCombat.csv").write_text("ID,ChrSpecializationID\n1,63\n", encoding="utf-8")
    (tmp_path / "retail-AssistedCombatStep.csv").write_text(
        "ID,OrderIndex,AssistedCombatID,SpellID\n2,1,1,133\n", encoding="utf-8",
    )
    (tmp_path / "retail-AssistedCombatRule.csv").write_text(
        "ID,OrderIndex,AssistedCombatStepID\n3,0,2\n", encoding="utf-8",
    )
    manifest = {"revision": "sample", "records": [
        {"path": "fire.apl.json", "source": "https://example.com/fire", "profile": {"priorityList": []}},
    ]}
    (tmp_path / "forever-apl-reference.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = tmp_path / "db.sqlite"
    counts = build_catalog(tmp_path, path)
    assert counts["retail"]["tables"]["SpellName"] == 1
    assert counts["forever"]["rotation_references"] == 1
    with closing(open_catalog(path)) as db:
        assert ability(db, "retail", 133)["SpellEffect"][0]["ID"] == "1234"
        assert ability(db, "titan", 133) == {"SpellName": [{"ID": "133", "Name_lang": "titan Fireball"}]}
        assert class_catalog(db, "retail", 8)["class_mask_spells"] == [{"id": 133, "name": "retail Fireball"}]
        assert rotation(db, "retail", 63, None)["steps"][0]["rules"][0]["ID"] == "3"
        assert rotation(db, "forever", None, "fire.apl.json")["reference"] == {"priorityList": []}
    (tmp_path / "titan-SpellName.csv").unlink()
    with pytest.raises(ValueError, match="missing required SpellName"):
        build_catalog(tmp_path, path)
    assert path.exists()


def test_macro_uses_localized_learned_spell_name(tmp_path):
    path = tmp_path / "WowAgent.lua"
    path.write_text('WowAgentDB = { combatProfile = { build = 70783, spells = { [133] = "火球术" } } }',
                    encoding="utf-8")
    profile = saved_profile(path)
    assert spell_macro(profile, 133) == "#showtooltip 火球术\n/cast [harm,nodead] 火球术"
    with pytest.raises(ValueError):
        spell_macro(profile, 116)
