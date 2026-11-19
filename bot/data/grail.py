"""Fill missing RetailQuestData fields from the project's pinned Grail snapshot."""
from __future__ import annotations

import fcntl
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
COMMIT = "6344558d9cdf17a8f46961b3e1118448162105af"
BASE = f"https://raw.githubusercontent.com/smaitch/Grail/{COMMIT}/Grail"
QUESTS_URL = f"{BASE}/Quests/Grail-Quests-MODERN.lua"
NPCS_URL = f"{BASE}/NPCs/Grail-NPCs-_retail_.lua"
NAMES_URLS = {
    locale: f"{BASE}/NPCs/Names/Grail-NPCs-_retail_-{locale}.lua"
    for locale in ("enUS", "zhCN")
}
CACHE = PROJECT / "data" / "grail" / COMMIT
ROLES = {"A": "pickup", "T": "turnIn"}


def _fetch(url: str) -> str:
    target = CACHE / url.rsplit("/", 1)[-1]
    if not target.is_file():
        with urllib.request.urlopen(url, timeout=30) as response:
            contents = response.read().decode("utf-8")
        _replace_text(target, contents)
    return target.read_text(encoding="utf-8")


def _replace_text(path: Path, contents: str) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as tmp:
        staged = Path(tmp.name)
        try:
            tmp.write(contents)
            tmp.close()
            staged.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def _quoted_values(text: str, key: int) -> list[str]:
    values = re.findall(rf"""^G\[{key}\]=(['"])((?:\\.|(?!\1)[^\r\n])*)\1\s*$""", text, re.M)
    return list(dict.fromkeys(re.sub(r"""\\(['"\\])""", r"\1", value) for _, value in values))


def parse_quest(text: str, quest_id: int) -> dict:
    """Keep plain NPC alternatives and conjunctions of quest prerequisites."""
    values = _quoted_values(text, quest_id)
    if not values:
        raise ValueError(f"grail_quest_not_found:{quest_id}")
    if len(values) != 1:
        raise ValueError(f"grail_quest_ambiguous:{quest_id}")
    info = {}
    for code, role in ROLES.items():
        associations = [token[2:] for token in values[0].split() if token.startswith(code + ":")]
        if associations and all(re.fullmatch(r"[1-9]\d*(?:,[1-9]\d*)*", value) for value in associations):
            info[role] = tuple(dict.fromkeys(int(npc) for value in associations for npc in value.split(",")))
    prereqs = [token[2:] for token in values[0].split() if token.startswith("P:")]
    if len(prereqs) == 1 and re.fullmatch(r"[1-9]\d*(?:\+[1-9]\d*)*", prereqs[0]):
        info["prerequisites"] = tuple(int(quest) for quest in prereqs[0].split("+"))
    return info


def parse_npc_point(text: str, npc_id: int, map_id: int) -> tuple[float, float] | None:
    tables = re.findall(rf"^G\[{npc_id}\]=\{{([^\r\n]*)\}}", text, re.M)
    points = set()
    for body in tables:
        for x, y in re.findall(rf"""['"]{map_id}:(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)['"]""", body):
            point = float(x), float(y)
            if all(0 < coordinate < 100 for coordinate in point):
                points.add(point)
    return next(iter(points)) if len(points) == 1 else None


def parse_name(text: str, npc_id: int) -> str | None:
    values = _quoted_values(text, npc_id)
    if len(values) == 1 and values[0] and not any(ord(char) < 32 or ord(char) == 127 for char in values[0]):
        return values[0]
    return None


def cached_npc_location(quest_id: int, map_id: int, role: str) -> tuple[int, tuple[float, float]] | None:
    """Read one unambiguous same-map association without downloading or writing data."""
    try:
        quests = (CACHE / QUESTS_URL.rsplit("/", 1)[-1]).read_text(encoding="utf-8")
        candidates = parse_quest(quests, quest_id).get(role, ())
        if len(candidates) != 1:
            return None
        npc = candidates[0]
        locations = (CACHE / NPCS_URL.rsplit("/", 1)[-1]).read_text(encoding="utf-8")
        point = parse_npc_point(locations, npc, map_id)
        if point is not None:
            return npc, (point[0] / 100, point[1] / 100)
    except (OSError, ValueError):
        return None
    return None


def collect(quest_id: int, map_id: int) -> dict:
    info = parse_quest(_fetch(QUESTS_URL), quest_id)
    quest = {"id": quest_id, "prerequisites": info.get("prerequisites", ())}
    entry = {"quest": quest, "npcs": {}, "names": {}, "unresolved": {}}
    if not (info.get("pickup") or info.get("turnIn")):
        return entry
    npcs = _fetch(NPCS_URL)
    for role in ROLES.values():
        candidates = info.get(role, ())
        if not candidates:
            continue
        # Ignore an ambiguous association even if only one candidate happens
        # to have coordinates in the source snapshot.
        npc = candidates[0]
        point = parse_npc_point(npcs, npc, map_id) if len(candidates) == 1 else None
        if point is None:
            entry["unresolved"][role] = candidates
            continue
        quest[role] = npc
        entry["npcs"][npc] = point
    if entry["npcs"]:
        locales = {locale: _fetch(url) for locale, url in NAMES_URLS.items()}
        for npc in entry["npcs"]:
            entry["names"][npc] = {
                locale: name for locale, source in locales.items() if (name := parse_name(source, npc))
            }
    return entry


def _load(lua: str):
    try:
        import lupa.lua51
    except ImportError as exc:
        raise ValueError("grail-sync 需要 Lua 5.1 校验器，请在项目环境安装 lupa") from exc
    runtime = lupa.lua51.LuaRuntime()
    runtime.execute("WowAgent = {}")
    try:
        runtime.execute(lua)
    except lupa.lua51.LuaError as exc:
        raise ValueError(f"grail_data_table_invalid: {exc}") from exc
    retail = runtime.globals().WowAgent.RetailQuestData
    if retail is None or retail.NPCPoint is None:
        raise ValueError("grail_data_table_invalid")
    return retail


def _insert_fields(lua: str, section: str, key: int, fields: str) -> str:
    start = lua.index(f"    {section} = {{")
    end = lua.index("\n    },", start)
    match = re.search(rf"(?m)^        \[{key}\] = \{{", lua[start:end])
    if match:
        index = start + match.end()
        rest = lua[index:]
        if rest.startswith("\n"):
            fields = "\n            " + fields + ","
        elif not rest.lstrip().startswith("}"):
            fields += ", "
        return lua[:index] + fields + rest
    block = f"\n        [{key}] = {{{fields}}},"
    return lua[:end] + block + lua[end:]


def _lua_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def merge_into_lua(lua: str, entry: dict) -> str:
    """Add only missing fields. Explicit false suppresses an upstream role."""
    retail = _load(lua)
    quest = entry["quest"]
    quest_id = quest["id"]
    existing = retail.quests[quest_id]
    fields = []
    accepted_npcs = set()
    for role in ROLES.values():
        npc = quest.get(role)
        if npc is None:
            continue
        configured = existing[role] if existing is not None else None
        if configured is None:
            fields.append(f"{role} = {npc}")
        if configured is None or configured == npc:
            accepted_npcs.add(npc)
    if not accepted_npcs:
        return lua
    prerequisites = quest.get("prerequisites")
    if prerequisites and (existing is None or existing.after is None):
        after = ", ".join(f"[{item}] = true" for item in prerequisites)
        fields.append(f"after = {{{after}}}")
    if fields:
        lua = _insert_fields(lua, "quests", quest_id, ", ".join(fields))
    for npc in sorted(accepted_npcs):
        if retail.npcs[npc] is None:
            x, y = entry["npcs"][npc]
            lua = _insert_fields(lua, "npcs", npc, f"{x}, {y}")
        current_names = retail.npcNames[npc]
        names = [
            f"{locale} = {_lua_string(name)}"
            for locale, name in sorted(entry["names"].get(npc, {}).items())
            if current_names is None or current_names[locale] is None
        ]
        if names:
            lua = _insert_fields(lua, "npcNames", npc, ", ".join(names))
    return lua


def bump_build(quest_lua: str, toc: str) -> tuple[str, str, int]:
    match = re.search(r"writeBuild\(14, (\d+) \+ navSequence", quest_lua)
    version = re.search(r"(?m)^## Version: (0)\.(\d+)\.(\d+)$", toc)
    if not match or not version or int(match[1]) != int(version[2]) * 100 + int(version[3]):
        raise ValueError("grail_build_and_toc_out_of_sync")
    build = int(match[1]) + 1
    if build >= 4096:
        raise ValueError("grail_build_overflow")
    quest_lua = quest_lua[:match.start(1)] + str(build) + quest_lua[match.end(1):]
    toc = toc[:version.start(3)] + str(int(version[3]) + 1) + toc[version.end(3):]
    return quest_lua, toc, build


def _commit(changes: dict[Path, tuple[str, str]]) -> None:
    written = []
    try:
        for path, (before, after) in changes.items():
            _replace_text(path, after)
            written.append((path, before))
    except OSError:
        for path, before in reversed(written):
            _replace_text(path, before)
        raise


def sync(quest_ids: list[int], *, map_id: int | None = None,
         deploy: bool = False, report_only: bool = False, missing_only: bool = False) -> dict:
    addon = PROJECT / "addon" / "WowAgent"
    data_path, quest_path, toc_path = (addon / name for name in ("RetailQuestData.lua", "Quest.lua", "WowAgent.toc"))
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / ".sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = merged = data_path.read_text(encoding="utf-8")
        retail = _load(original)
        if map_id is None:
            map_id = retail.map
        if map_id != retail.map:
            raise ValueError(f"grail_map_unsupported:{map_id} (RetailQuestData 只支持地图 {retail.map})")
        entries = []
        for quest_id in dict.fromkeys(quest_ids):
            if quest_id <= 0:
                raise ValueError(f"grail_invalid_quest:{quest_id}")
            existing = retail.quests[quest_id]
            if missing_only and existing is not None and all(
                (npc := existing[role]) is False or (npc is not None and retail.npcs[npc] is not None)
                for role in ROLES.values()
            ):
                continue
            entry = collect(quest_id, map_id)
            entries.append(entry)
            merged = merge_into_lua(merged, entry)
        result = {"entries": entries, "written": False, "changes": merged != original}
        if not result["changes"] or report_only:
            return result
        _load(merged)
        quest_lua = quest_path.read_text(encoding="utf-8")
        toc = toc_path.read_text(encoding="utf-8")
        next_lua, next_toc, build = bump_build(quest_lua, toc)
        _commit({data_path: (original, merged), quest_path: (quest_lua, next_lua), toc_path: (toc, next_toc)})
        result.update(written=True, build=build)
        if deploy:
            game = Path("/Volumes/Mac SSD1/Applications/World of Warcraft/_retail_/Interface/AddOns/WowAgent")
            if not game.is_dir():
                raise FileNotFoundError(f"插件数据已写入仓库。游戏插件目录不存在：{game}")
            shutil.copytree(addon, game, dirs_exist_ok=True)
        return result
