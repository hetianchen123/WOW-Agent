"""Exact-build Retail talent presets and reviewed guide import strings."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..core.paths import CONFIG_DIR

GUIDES = CONFIG_DIR / "retail-talent-builds.json"
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CONTEXTS = {"solo", "raid", "cleave", "mythic"}


def guide_builds(path: Path = GUIDES) -> dict:
    guides = json.loads(path.read_text(encoding="utf-8"))
    for spec_id, guide in guides["specs"].items():
        if set(guide["codes"]) != CONTEXTS:
            raise ValueError(f"推荐配置的玩法覆盖不完整 {spec_id}")
        for context, code in guide["codes"].items():
            if not re.fullmatch(r"[A-Za-z0-9+/]{26,}", code):
                raise ValueError(f"无效的推荐配置 {spec_id}/{context}")
            header = sum(ALPHABET.index(char) << (6 * index) for index, char in enumerate(code[:4]))
            if header & 255 != 2 or header >> 8 != int(spec_id):
                raise ValueError(f"推荐配置的专精或格式不匹配 {spec_id}/{context}")
    return guides


def retail_talents(db, spec_name: str | None = None) -> dict:
    edition = db.execute("SELECT build FROM edition WHERE key='retail'").fetchone()
    if not edition:
        raise ValueError("数据库没有正式服数据")
    build = edition[0]
    guides = guide_builds()
    compatible = guides["patch"] == build.rsplit(".", 1)[0]
    presets = defaultdict(list)
    ranks = defaultdict(list)
    specs = {}
    for dataset, id_, payload in db.execute("""
        SELECT dataset, record_id, payload FROM record
        WHERE edition='retail' AND dataset IN ('ChrSpecialization','TraitTreeLoadout','TraitTreeLoadoutEntry')
        ORDER BY dataset, line
    """):
        record = json.loads(payload)
        if dataset == "ChrSpecialization":
            specs[id_] = record
        elif dataset == "TraitTreeLoadout":
            spec = int(record["ChrSpecializationID"])
            presets[spec].append({"id": id_, "tree": int(record["TraitTreeID"])})
        else:
            ranks[int(record["TraitTreeLoadoutID"])].append(record)

    result = []
    for spec in sorted(presets):
        if spec not in specs:
            continue
        data = specs[spec]
        guide = guides["specs"].get(str(spec))
        names = {data["Name_lang"].casefold(), (guide or {}).get("name", "").casefold()}
        if spec_name and spec_name.strip().casefold() not in names:
            continue
        for preset in presets[spec]:
            records = ranks[preset["id"]]
            preset["rank_rows"] = len(records)
            if spec_name:
                preset["entries"] = sorted(records, key=lambda entry: int(entry["OrderIndex"]))
        result.append({"id": spec, "name": data["Name_lang"], "class_id": int(data["ClassID"]),
                       "client_presets": presets[spec], "guide": guide if compatible else None})
    if spec_name and not result:
        raise ValueError(f"正式服天赋库中找不到专精 {spec_name}")
    return {"build": build, "guide_patch": guides["patch"], "guide_min_level": guides["min_level"],
            "guide_source": guides["source_name"], "specializations": result}


def export_addon_guides(db, output: Path) -> None:
    catalog = retail_talents(db)
    patch = catalog["build"].rsplit(".", 1)[0]
    if patch != catalog["guide_patch"]:
        raise ValueError("推荐天赋的补丁与数据库不一致")

    def quote(text):
        return json.dumps(text, ensure_ascii=False)

    lines = [
        "-- Generated from config/retail-talent-builds.json by bot.cli combat talents.",
        "WowAgent.RetailTalentGuides = {",
        f"    patch = {quote(patch)},",
        f"    minLevel = {catalog['guide_min_level']},",
        f"    sourceName = {quote(catalog['guide_source'])},",
        "    specs = {",
    ]
    for spec in catalog["specializations"]:
        guide = spec["guide"]
        if guide is None:
            continue
        lines.extend([
            f"        [{spec['id']}] = {{",
            f"            hero = {quote(guide['hero'])},",
            "            codes = {",
        ])
        for context, code in guide["codes"].items():
            lines.append(f"                {context} = {quote(code)},")
        lines.extend(["            },", "        },"])
    lines.extend(["    },", "}", ""])
    output.write_text("\n".join(lines), encoding="utf-8")
