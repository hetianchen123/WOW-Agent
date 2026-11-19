"""Validate Grail import with offline samples of the pinned snapshot format."""
from pathlib import Path
import shutil

import pytest

from bot.data import grail

QUESTS = """
G[90898]='FH K4096 L10 T:244715 P:90896 e11 E66192'
G[90896]='K4096 A:244666 T:244666 P:90895+90893'
G[90911]='FH K4096 L10 A:244714 P:90897 e11 E66192'
G[90999]='FH K4096 L10 A:244714 e11'
G[91000]='A:244714,244715 P:90898+PN200'
G[91001]='T:244717'
"""
NPCS = """
G[244666]={'2451:24.58,37.07'}
G[244715]={'2451:69.14,34.63'}
G[244714]={'2451:19.55,61.59', '12:10.0,10.0'}
G[244717]={'2451:19.55,61.59', '2451:10.0,10.0'}
G[99999]={'12:10.0,10.0'}
"""
NAMES_EN = """
G[244715]='Thrall'
G[244714]="Falconer's Assistant"
"""
NAMES_ZH = """
G[244715]='萨尔'
"""


def test_parse_quest_associations():
    assert grail.parse_quest(QUESTS, 90898) == {"turnIn": (244715,), "prerequisites": (90896,)}
    assert grail.parse_quest(QUESTS, 90999) == {"pickup": (244714,)}
    assert grail.parse_quest(QUESTS, 90896)["prerequisites"] == (90895, 90893)
    assert grail.parse_quest(QUESTS, 91000) == {"pickup": (244714, 244715)}
    with pytest.raises(ValueError, match="grail_quest_not_found"):
        grail.parse_quest(QUESTS, 1)


def test_parse_npc_point_requires_unique_point_on_map():
    assert grail.parse_npc_point(NPCS, 244715, 2451) == (69.14, 34.63)
    assert grail.parse_npc_point(NPCS, 244714, 2451) == (19.55, 61.59)
    assert grail.parse_npc_point(NPCS, 244715, 12) is None
    assert grail.parse_npc_point(NPCS, 244717, 2451) is None


def test_parse_locale_names():
    assert grail.parse_name(NAMES_ZH, 244715) == "萨尔"
    assert grail.parse_name(NAMES_EN, 244714) == "Falconer's Assistant"
    assert grail.parse_name(NAMES_ZH, 244714) is None


def test_runtime_npc_location_uses_read_only_cache_on_each_supported_map(tmp_path, monkeypatch):
    monkeypatch.setattr(grail, "CACHE", tmp_path)
    assert grail.cached_npc_location(90911, 12, "pickup") is None
    (tmp_path / grail.QUESTS_URL.rsplit("/", 1)[-1]).write_text(QUESTS, encoding="utf-8")
    (tmp_path / grail.NPCS_URL.rsplit("/", 1)[-1]).write_text(NPCS, encoding="utf-8")
    assert grail.cached_npc_location(90911, 12, "pickup") == (244714, (.1, .1))
    assert grail.cached_npc_location(90911, 2248, "pickup") is None
    assert grail.cached_npc_location(91000, 2451, "pickup") is None
    assert grail.cached_npc_location(91001, 2451, "turnIn") is None


@pytest.fixture
def source(monkeypatch):
    sample = {
        grail.QUESTS_URL: QUESTS,
        grail.NPCS_URL: NPCS,
        grail.NAMES_URLS["enUS"]: NAMES_EN,
        grail.NAMES_URLS["zhCN"]: NAMES_ZH,
    }
    monkeypatch.setattr(grail, "_fetch", sample.__getitem__)


@pytest.fixture
def project(tmp_path, monkeypatch):
    addon = tmp_path / "addon" / "WowAgent"
    addon.mkdir(parents=True)
    for name in ("RetailQuestData.lua", "Quest.lua", "WowAgent.toc"):
        shutil.copy2(Path("addon/WowAgent") / name, addon)
    monkeypatch.setattr(grail, "PROJECT", tmp_path)
    monkeypatch.setattr(grail, "CACHE", tmp_path / "cache")
    return addon


def test_merge_keeps_existing_fields_and_respects_disabled_upstream_role(source):
    lua = Path("addon/WowAgent/RetailQuestData.lua").read_text(encoding="utf-8")
    assert grail.merge_into_lua(lua, grail.collect(90911, 2451)) == lua
    assert grail.merge_into_lua(lua, grail.collect(90896, 2451)) == lua

    entry = grail.collect(90999, 2451)
    merged = grail.merge_into_lua(lua, entry)
    retail = grail._load(merged)
    assert retail.quests[90999].pickup == 244714
    assert retail.npcNames[244714].enUS == "Falconer's Assistant"
    assert retail.npcNames[244714].zhCN is None
    assert retail.quests[90896].respawnNames.zhCN == "罗格洛克"
    assert grail.merge_into_lua(merged, entry) == merged


def test_sync_supports_turn_in_only_and_bumps_once(project, source):
    data = project / "RetailQuestData.lua"
    lua = data.read_text(encoding="utf-8").replace("turnIn = 244715, after = {[90896]", "after = {[90896]")
    data.write_text(lua, encoding="utf-8")
    toc = project / "WowAgent.toc"
    original_toc = toc.read_text(encoding="utf-8")
    original_quest = (project / "Quest.lua").read_text(encoding="utf-8")
    _, next_toc, next_build = grail.bump_build(original_quest, original_toc)

    report = grail.sync([90898, 90999], report_only=True)
    assert report["changes"] and not report["written"]
    assert data.read_text(encoding="utf-8") == lua and toc.read_text(encoding="utf-8") == original_toc

    result = grail.sync([90898, 90999, 90898])
    assert result["written"] and result["build"] == next_build
    assert toc.read_text(encoding="utf-8") == next_toc
    retail = grail._load(data.read_text(encoding="utf-8"))
    assert retail.quests[90898].turnIn == 244715 and retail.quests[90898].pickup == 244666
    assert retail.quests[90999].pickup == 244714
    assert not grail.sync([90898, 90999])["changes"]


def test_sync_does_not_fetch_complete_tracked_quest(project, monkeypatch):
    def unexpected_fetch(_):
        pytest.fail("complete tracked quest attempted to fetch Grail")

    monkeypatch.setattr(grail, "_fetch", unexpected_fetch)
    assert not grail.sync([90896], map_id=2451, missing_only=True)["entries"]
    with pytest.raises(ValueError, match="grail_map_unsupported"):
        grail.sync([90896], map_id=2248, missing_only=True)


def test_sync_leaves_data_unchanged_when_build_is_inconsistent(project, source):
    data = project / "RetailQuestData.lua"
    original = data.read_text(encoding="utf-8")
    (project / "WowAgent.toc").write_text("## Version: 0.2.78\n")
    with pytest.raises(ValueError, match="grail_build_and_toc_out_of_sync"):
        grail.sync([90999])
    assert data.read_text(encoding="utf-8") == original


def test_collect_keeps_ambiguous_associations_out_of_writes(project, source):
    multiple = grail.collect(91000, 2451)
    assert multiple["unresolved"] == {"pickup": (244714, 244715)} and not multiple["npcs"]
    phased = grail.collect(91001, 2451)
    assert phased["unresolved"] == {"turnIn": (244717,)} and not phased["npcs"]
    assert not grail.sync([91000, 91001])["changes"]
