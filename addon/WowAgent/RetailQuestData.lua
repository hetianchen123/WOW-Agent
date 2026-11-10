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
        [90888] = {pickup = 244655, turnIn = 244658, after = {[90886] = true, [90887] = true}},
        [90893] = {pickup = 244657, turnIn = 244666, after = {[90888] = true}},
        [90895] = {pickup = 244658, turnIn = 244667, after = {[90888] = true}},
        [90896] = {pickup = 244666, turnIn = 244666, after = {[90893] = true, [90895] = true}},
    },
    npcs = {
        [244643] = {69.98, 40.66},
        [244655] = {56.42, 59.73},
        [244656] = {56.33, 59.57},
        [244657] = {19.36, 57.44},
        [244658] = {19.46, 57.43},
        [244666] = {24.58, 37.07},
        [244667] = {24.66, 37.11},
        [244729] = {56.44, 59.54},
    },
}
