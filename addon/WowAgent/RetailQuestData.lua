-- Grail Retail sample, Siege of Arathi Highlands. Coordinates are percentages.
-- Upstream snapshot: https://github.com/smaitch/Grail/tree/6344558d9cdf17a8f46961b3e1118448162105af
-- See RetailQuestData.LICENSE.txt. Prerequisites below are candidate hints;
-- quest availability is checked against the current character's game state.
WowAgent.RetailQuestData = {
    map = 2451,
    quests = {
        [90882] = {pickup = 244643, turnIn = 244643},
        [90883] = {pickup = 244643, turnIn = 244729, after = {[90882] = true}},
        [90885] = {
            pickup = 244729, turnIn = 244656, after = {[90883] = true},
            -- Interaction profile from the quest's collection objective.
            objectNames = {enUS = "Prized Pumpkin", zhCN = "稀有的南瓜"},
        },
        [90886] = {pickup = 244656, turnIn = 244656, after = {[90883] = true}},
        [90887] = {pickup = 244655, turnIn = 244655, after = {[90883] = true}},
        [90888] = {pickup = 244655, turnIn = 244658, arrival = true, after = {[90886] = true, [90887] = true}},
        [90893] = {
            pickup = 244657, turnIn = 244666, resource = true, after = {[90888] = true},
            -- The tracker hides the individual contributors behind the progress bar.
            -- English objective data: https://www.wowhead.com/quest=90893/repelling-the-siege
            resourceNames = {
                enUS = {["Gnoll Prowler"] = true, ["Gnoll Biter"] = true, ["Ogre Basher"] = true,
                    ["Kobold Waxmancer"] = true},
                zhCN = {["豺狼人徘徊者"] = true, ["豺狼人撕咬者"] = true, ["食人魔重击者"] = true,
                    ["狗头人蜡术士"] = true},
            },
        },
        [90895] = {
            pickup = 244658, turnIn = 244667, after = {[90888] = true},
            -- Counted by the client, but completed by using the rune at a catapult.
            objectCreature = 249269,
            objectNames = {enUS = "Worn Catapult"},
        },
        [90896] = {
            pickup = 244666, turnIn = 244666, after = {[90893] = true, [90895] = true},
            -- zhCN task tracker and user observation, 2026-11-18; spawn ID not yet observed.
            respawnNames = {zhCN = "罗格洛克"},
        },
        [90898] = {
            -- WowHead series: ... 90893 -> 90895 -> 90896 -> 90898 "Back to
            -- Hammerfall" -> 90911. `after` lists prerequisites of this quest.
            -- Turn-in is the Hammerfall Thrall spawn, not the siege-camp one:
            -- Grail G[90898]='T:244715 P:90896', G[244715]={'2451:69.14,34.63'}
            -- (cross-checked with the live pin 2026-11-19). Creature-ID
            -- matching rejected the wrong 244666 as "never selected".
            pickup = 244666, turnIn = 244715, after = {[90896] = true},
        },
        [90911] = {
            -- Follow-up accepted at Hammerfall in build 278. Grail's pickup
            -- points to the other branch, so use the live offer/map pin here.
            pickup = false, after = {[90898] = true},
            choice = {fromMap = 2451, giver = 244715, point = {69.20, 34.40}, gossip = 133894,
                dialog = 902, response = 1701, map = 85, nextQuest = 91281},
        },
        -- Orgrimmar: walking up to the projection grants the objective credit.
        [91281] = {map = 85, arrival = true, turnIn = 241677, turnInPoint = {53.26, 77.15}},
        [88719] = {map = 2432, turnIn = 236692, turnInPoint = {48.50, 38.41},
            choice = {fromMap = 85, giver = 241677, point = {53.26, 77.15}, gossip = 133521, map = 2432,
                retryStanding = true}},
        -- The scenario updates the objective while Liadrin walks to the plaza.
        [86769] = {map = 2432, arrival = true},
    },
    npcs = {
        [244643] = {69.98, 40.66},
        [244655] = {56.42, 59.73},
        [244656] = {56.33, 59.57},
        [244657] = {19.36, 57.44},
        [244658] = {19.46, 57.43},
        [244666] = {24.58, 37.07},
        [244667] = {24.66, 37.11},
        [244715] = {69.14, 34.63},
        [244729] = {56.44, 59.54},
    },
    npcNames = {
        -- Farmer's Nemesis, quest 90887, links this spawn as both giver and receiver.
        [244655] = {enUS = "Lady Jaina Proudmoore", zhCN = "吉安娜·普罗德摩尔"},
        [244656] = {enUS = "Thrall", zhCN = "萨尔"},
        [244657] = {enUS = "Thrall", zhCN = "萨尔"},
        [244658] = {enUS = "Lady Jaina Proudmoore", zhCN = "吉安娜·普罗德摩尔"},
        [244666] = {enUS = "Thrall", zhCN = "萨尔"},
        [244667] = {enUS = "Lady Jaina Proudmoore", zhCN = "吉安娜·普罗德摩尔"},
        [244715] = {enUS = "Thrall", zhCN = "萨尔"},
        [244729] = {enUS = "Farmer Bruvk", zhCN = "农夫布鲁沃克"},
        [241677] = {zhCN = "女伯爵莉亚德琳的影像"},
        [236692] = {zhCN = "女伯爵莉亚德琳"},
    },
}

-- A quest-level role point overrides the NPC's default spawn coordinates.
function WowAgent.RetailQuestData.NPCPoint(data, npc, role)
    return data and (data[role .. "Point"] or (npc and WowAgent.RetailQuestData.npcs[npc])) or nil
end
