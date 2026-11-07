from bot.data.savedvars import journalator_sales, parse_lua_table


SAMPLE = """
-- Journalator SavedVariables demo
JOURNALATOR_LOGS = {
    ["sales"] = {
        [1] = {
            ["price"] = 125000,
            ["quantity"] = 20,
            ["name"] = "深流鳟鱼",
            ["timestamp"] = 1762000000,
        },
        [2] = {
            ["price"] = 90000,
            ["quantity"] = 10,
            ["name"] = "沥青黑鲈",
            ["timestamp"] = 1762100000,
        },
    },
    ["settings"] = { ["enabled"] = true, ["threshold"] = 0.5, },
    ["nothing"] = nil,
}
GOLD = 123456
"""


def test_parse_nested_tables():
    data = parse_lua_table(SAMPLE)
    logs = data["JOURNALATOR_LOGS"]
    assert isinstance(logs, dict)
    sales = logs["sales"]
    assert sales[1]["price"] == 125000
    assert sales[1]["quantity"] == 20
    assert sales[2]["name"] == "沥青黑鲈"
    assert logs["settings"]["enabled"] is True
    assert logs["settings"]["threshold"] == 0.5
    assert data["GOLD"] == 123456


def test_parse_strings_with_escapes_and_arrays():
    text = r"""
X = {
    ["msg"] = "he said \"hi\"\nbye",
    ["path"] = 'C:\\WoW\\Interface',
    list = { "a", "b", 3, true },
}
"""
    data = parse_lua_table(text)
    x = data["X"]
    assert x["msg"] == 'he said "hi"\nbye'
    assert x["path"] == "C:\\WoW\\Interface"
    assert x["list"] == ["a", "b", 3, True]


def test_journalator_sales_extract():
    data = parse_lua_table(SAMPLE)
    rows = journalator_sales(data)
    assert len(rows) == 2
    assert rows[0]["price"] == 125000
