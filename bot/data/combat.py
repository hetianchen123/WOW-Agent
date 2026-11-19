"""Versioned catalogue built from exact-build client tables and sourced notes."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from ..core.paths import DATA_DIR

COMBAT_DB = DATA_DIR / "combat.sqlite3"
SOURCE_DIR = DATA_DIR / "combat-sources"
EDITIONS = {
    "retail": ("正式服", "12.1.5.70783"),
    "titan": ("时光服", "3.80.2.70522"),
    "forever": ("无限服", "1.60.1.70790"),
}
DB2_TABLES = (
    "ChrClasses", "ChrRaces", "ChrSpecialization", "SkillLine", "SkillLineAbility",
    "SpellName", "Spell", "SpellEffect", "SpellClassOptions", "SpellCooldowns", "SpellPower",
    "SpellRange", "SpellCategories", "Talent", "TalentTab", "TraitTree", "TraitNode", "TraitNodeEntry",
    "TraitNodeXTraitNodeEntry", "TraitDefinition", "TraitEdge", "TraitTreeLoadout",
    "TraitTreeLoadoutEntry", "AssistedCombat", "AssistedCombatRule", "AssistedCombatStep",
)
SPELL_LINKS = ("SpellEffect", "SpellClassOptions", "SpellCooldowns", "SpellPower", "SpellCategories")
NOTES = (
    ("titan", "https://wow.blizzard.cn/news/trr/20260113/43039_1281511.html",
     "法师", "冰霜", "时光服调整新增冰冻宝珠与冷锋，短冷却和触发改变了优先级，另保留霜火之箭路线。"),
    ("titan", "https://wow.blizzard.cn/news/trr/2026052/43039_1302114.html",
     "圣骑士", "惩戒", "强化圣光裁决影响引导时可使用的功能技能，不能照搬巫妖王原版循环。"),
    ("titan", "https://wow.blizzard.cn/news/20260102/40565_1279877.html",
     "潜行者", "敏锐", "死亡印记引入新的终结技窗口，出血与锁喉天赋也有独立改动。"),
    ("titan", "https://wow.blizzard.cn/news/20251203/40565_1274343.html",
     "萨满祭司", "增强", "双手与双持路线的可用技能和触发不同，双手武器的熔岩强击替换熔岩猛击。"),
    ("retail", "https://worldofwarcraft.blizzard.com/en-us/news/24246290/",
     "", "", "输出执行可选游戏的一键辅助动作。运行前由客户端核对动作栏技能。"),
    ("forever", "https://wow.blizzard.cn/news/24303862/index.html",
     "", "", "独立职业和天赋改动。客户端天赋数据与可执行输出配置分别管理。"),
)

SCHEMA = """
CREATE TABLE edition(key TEXT PRIMARY KEY, label TEXT NOT NULL, build TEXT NOT NULL UNIQUE);
CREATE TABLE source(
    edition TEXT NOT NULL REFERENCES edition(key), dataset TEXT NOT NULL,
    url TEXT NOT NULL, sha256 TEXT NOT NULL, row_count INTEGER NOT NULL,
    PRIMARY KEY(edition, dataset)
);
CREATE TABLE record(
    edition TEXT NOT NULL, dataset TEXT NOT NULL, line INTEGER NOT NULL,
    record_id INTEGER, spell_id INTEGER, label TEXT, payload TEXT NOT NULL,
    PRIMARY KEY(edition, dataset, line),
    FOREIGN KEY(edition, dataset) REFERENCES source(edition, dataset)
);
CREATE INDEX record_id_idx ON record(edition, dataset, record_id);
CREATE INDEX record_spell_idx ON record(edition, dataset, spell_id) WHERE spell_id IS NOT NULL;
CREATE TABLE rotation_note(
    edition TEXT NOT NULL REFERENCES edition(key), class_name TEXT NOT NULL, spec TEXT NOT NULL,
    url TEXT NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE rotation_reference(
    edition TEXT NOT NULL REFERENCES edition(key), path TEXT NOT NULL,
    revision TEXT NOT NULL, url TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(edition, path)
);
"""


def client_edition(build: int) -> str | None:
    # Edition identity observed in local live telemetry, not a claim that the
    # exact-build catalogue below has been reimported. Inputs still require the
    # current client's action binding/range/receipt verification.
    if build == 70961:
        return "retail"
    if build == 70959:
        return "forever"
    return next((key for key, (_, full) in EDITIONS.items()
                 if int(full.rsplit(".", 1)[1]) == build), None)


def _import_csv(db: sqlite3.Connection, edition: str, build: str, table: str, file: Path) -> None:
    url = f"https://wago.tools/db2/{table}/csv?build={quote(build)}"
    checksum = hashlib.sha256()
    with file.open("rb") as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
            checksum.update(chunk)
    with file.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "ID" not in reader.fieldnames:
            raise ValueError(f"{file}: missing DB2 ID column")
        db.execute("INSERT INTO source VALUES (?, ?, ?, ?, 0)",
                   (edition, table, url, checksum.hexdigest()))

        def rows():
            for line, row in enumerate(reader, 1):
                if None in row or None in row.values():
                    raise ValueError(f"{file}: invalid CSV record {line}")
                name = row.get("Name_lang") or row.get("Name") or row.get("Filename")
                spell = row.get("SpellID") if table in SPELL_LINKS else None
                yield edition, table, line, int(row["ID"]), int(spell) if spell else None, name, json.dumps(row, ensure_ascii=False)

        count = db.executemany("INSERT INTO record VALUES (?, ?, ?, ?, ?, ?, ?)", rows()).rowcount
        db.execute("UPDATE source SET row_count = ? WHERE edition = ? AND dataset = ?",
                   (count, edition, table))


def build_catalog(source_dir: Path = SOURCE_DIR, path: Path = COMBAT_DB) -> dict:
    """Publish a complete SQLite file atomically. Missing optional tables stay visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"combat sources not found: {source_dir}")
    with NamedTemporaryFile(dir=path.parent, prefix=".combat-", delete=False) as file:
        temp = Path(file.name)
    try:
        with closing(sqlite3.connect(temp)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            for edition, (label, build) in EDITIONS.items():
                db.execute("INSERT INTO edition VALUES (?, ?, ?)", (edition, label, build))
                for table in DB2_TABLES:
                    file = source_dir / f"{edition}-{table}.csv"
                    if file.is_file():
                        _import_csv(db, edition, build, table, file)
                if db.execute("SELECT row_count FROM source WHERE edition=? AND dataset='SpellName'",
                              (edition,)).fetchone() in (None, (0,)):
                    raise ValueError(f"{edition}: missing required SpellName data")
            db.executemany("INSERT INTO rotation_note VALUES (?, ?, ?, ?, ?)",
                           ((key, cls, spec, url, note) for key, url, cls, spec, note in NOTES))
            references = source_dir / "forever-apl-reference.json"
            if references.is_file():
                manifest = json.loads(references.read_text(encoding="utf-8"))
                revision = manifest["revision"]
                db.executemany(
                    "INSERT INTO rotation_reference VALUES ('forever', ?, ?, ?, ?)",
                    ((entry["path"], revision, entry["source"], json.dumps(entry["profile"]))
                     for entry in manifest["records"]),
                )
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("catalog integrity check failed")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return coverage(path)


def open_catalog(path: Path = COMBAT_DB) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def coverage(path: Path = COMBAT_DB) -> dict:
    with closing(open_catalog(path)) as db:
        result = {key: {"build": build, "tables": {}}
                  for key, build in db.execute("SELECT key, build FROM edition")}
        for key, table, count in db.execute("SELECT edition, dataset, row_count FROM source"):
            result[key]["tables"][table] = count
        for key, count in db.execute("SELECT edition, count(*) FROM rotation_note GROUP BY edition"):
            result[key]["rotation_notes"] = count
        for key, count in db.execute("SELECT edition, count(*) FROM rotation_reference GROUP BY edition"):
            result[key]["rotation_references"] = count
    return result
