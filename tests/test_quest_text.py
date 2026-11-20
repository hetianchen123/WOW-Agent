from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bot.decision.quest_modes import executable_objective_mode
from bot.perception import channel_spec as pixels
from bot.perception.quest_text import QuestText, QuestTextStream, decode_quest_text
from bot.perception.travel_channel import ObjectiveAction, TaskFlags


def render_pages(text, *, quest_id=95998, objective=2, sequence=35):
    raw = text.encode("utf-8")
    checksum = 0
    for byte in raw:
        checksum = (checksum * 31 + byte) % 1048576
    for offset in range(0, len(raw), 16):
        chunk = raw[offset:offset + 16].ljust(16, b"\0")
        row_values = [0xC71 + sequence * 4096, quest_id, len(raw) + offset // 16 * 512 + objective * 16384,
                      checksum]
        row_values.extend(int.from_bytes(chunk[i:i + 2], "little") + (sequence % 16) * 65536
                          for i in range(0, 16, 2))
        image = np.zeros((192, 142, 3), dtype=np.uint8)
        for i, word in enumerate(row_values):
            pixels.render_frame(image, 0, i * 16, pixels.encode_frame(i, word))
        yield image


def test_quest_context_reassembles_utf8_and_rejects_mixed_target_or_torn_rows():
    objective = "获得强化休息增益效果"
    images = list(render_pages("广阔天地\x1fobject\x1f" + objective + "\x1f在篝火旁坐下"))
    pages = [decode_quest_text(image) for image in images]
    torn = images[0].copy()
    pixels.render_frame(torn, 0, 64, pixels.encode_frame(4, 4 * 65536))
    assert decode_quest_text(torn) is None

    nav = SimpleNamespace(sequence=35, quest_id=95998, objective_index=2, map_id=1429)
    stream = QuestTextStream()
    stream.observe(replace(pages[0], ordinal=1), nav)
    assert stream.identity is None
    for page in reversed(pages[1:]):
        stream.observe(page, nav)
    assert stream.text is None
    stream.observe(pages[0], nav)
    assert stream.text == QuestText("广阔天地", "object", objective, "在篝火旁坐下")


def test_native_quest_description_is_read_for_current_id_and_previous_selection_is_restored():
    lupa = pytest.importorskip("lupa.lua51")
    lua = lupa.LuaRuntime()
    lua.execute('''
        WowAgent, WowAgentChannelFrame, UIParent, rows = {}, {}, {}, {}
        local noop = function() end
        function CreateFrame()
            return {SetSize=noop, SetPoint=noop, SetFrameStrata=noop, Show=noop, Hide=noop}
        end
        function WowAgent.NewPixelRow(_, offset)
            return function(kind, value) rows[offset / 16] = {kind, value} end
        end
        selected, activeOffer, logReads = 47, 60, 0
        C_QuestLog = {
            GetTitleForQuestID = function(id) return id == 62 and '法戈第矿洞' or '交易' end,
            GetSelectedQuest = function() return selected end,
            SetSelectedQuest = function(id) selected = id end,
        }
        function GetQuestID() return activeOffer end
        function GetQuestText() return '交给威廉小雕像' end
        function GetQuestLogQuestText()
            logReads = logReads + 1
            return selected == 62 and '前往法戈第矿洞侦察' or '另一项任务'
        end
    ''')
    lua.execute(Path("addon/WowAgent/QuestText.lua").read_text())
    lua.execute("WowAgent.RememberQuestDescription(47); WowAgent.RememberQuestDescription(60)")
    for quest_id, objective, expected in [(62, "侦察法戈第矿洞", "前往法戈第矿洞侦察"),
                                         (60, "寻找小雕像", "交给威廉小雕像")]:
        stream = QuestTextStream()
        for sequence in range(1, 25):
            lua.globals().WowAgent.WriteQuestText(sequence, quest_id, lua.table(type="event", text=objective), 1, False)
            image = np.zeros((192, 142, 3), dtype=np.uint8)
            for index in range(12):
                kind, value = lua.globals().rows[index][1], lua.globals().rows[index][2]
                pixels.render_frame(image, 0, index * 16, pixels.encode_frame(kind, value))
            nav = SimpleNamespace(sequence=sequence, quest_id=quest_id, objective_index=1, map_id=1429)
            stream.observe(decode_quest_text(image), nav)
            if stream.text is not None:
                break
        assert stream.text.description == expected
        assert lua.globals().selected == 47
    assert lua.globals().logReads == 1


def test_selected_mode_requires_native_target_telemetry_and_local_handler():
    nav = SimpleNamespace(quest_id=70001, map_id=1429, destination_map=1429, destination=(.4, .6),
                          objective_action=ObjectiveAction.NONE, objective_target=0, task=TaskFlags.PREPARED)
    assert executable_objective_mode("kill", nav, measurable=True, targets=()) is None
    assert executable_objective_mode("camp_rest", nav, measurable=True, targets=()) is None
    assert executable_objective_mode("choice", nav, measurable=True, targets=()) is None
    assert executable_objective_mode("arrive", nav, measurable=True, targets=()) == (ObjectiveAction.ARRIVE, False)
    nav.objective_target = 491
    assert executable_objective_mode("loot_drop", nav, measurable=True, targets=()) == (ObjectiveAction.COMBAT, True)
