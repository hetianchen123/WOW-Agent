"""Command-line access to combat data, character profiles, and one-target tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .core.config import load_config
from .data.combat import (
    COMBAT_DB, EDITIONS, SOURCE_DIR, SPELL_LINKS, build_catalog, client_edition, coverage, open_catalog,
)
from .data.savedvars import parse_lua_table
from .perception.capture import CaptureError, ScreenCapture
from .perception.combat_channel import combat_region, decode_combat


def saved_profile(path: Path) -> dict:
    raw = parse_lua_table(path.read_text(encoding="utf-8"))
    profile = (raw.get("WowAgentDB") or {}).get("combatProfile")
    if not isinstance(profile, dict) or not client_edition(profile.get("build", 0)):
        raise ValueError("缺少受支持的角色快照。游戏内执行 /wagent combat profile 后重载。")
    return profile


def spell_macro(profile: dict, spell_id: int) -> str:
    name = profile.get("spells", {}).get(spell_id)
    if not isinstance(name, str) or not name or any(c in name for c in "\r\n;[]"):
        raise ValueError(f"角色法术书中没有可用于宏的技能 {spell_id}")
    body = f"#showtooltip {name}\n/cast [harm,nodead] {name}"
    if len(body.encode("utf-8")) > 255:
        raise ValueError("宏超过 255 字节")
    return body


def ability(db, edition, spell_id):
    tables = ("SpellName", "Spell", *SPELL_LINKS)
    result = {}
    for table in tables:
        field = "spell_id" if table in SPELL_LINKS else "record_id"
        rows = db.execute(
            f"SELECT payload FROM record WHERE edition=? AND dataset=? AND {field}=? ORDER BY line",
            (edition, table, spell_id),
        )
        records = [json.loads(row[0]) for row in rows]
        if records:
            result[table] = records
    return result


def class_catalog(db, edition, class_id):
    row = db.execute(
        "SELECT payload FROM record WHERE edition=? AND dataset='ChrClasses' AND record_id=?",
        (edition, class_id),
    ).fetchone()
    if row is None or not 1 <= class_id <= 63:
        raise ValueError(f"版本 {edition} 找不到职业 {class_id}")
    info = json.loads(row[0])
    specs = db.execute("""
        SELECT payload FROM record WHERE edition=? AND dataset='ChrSpecialization'
          AND CAST(json_extract(payload, '$.ClassID') AS INTEGER)=? ORDER BY line
    """, (edition, class_id)).fetchall()
    trees = db.execute("""
        SELECT payload FROM record WHERE edition=? AND dataset='TalentTab'
          AND CAST(json_extract(payload, '$.ClassMask') AS INTEGER) & ? <> 0 ORDER BY line
    """, (edition, 1 << (class_id - 1))).fetchall()
    skills = db.execute("""
        SELECT DISTINCT n.record_id, n.label
        FROM record a CROSS JOIN record n
        WHERE a.edition=? AND a.dataset='SkillLineAbility'
          AND CAST(json_extract(a.payload, '$.ClassMask') AS INTEGER) & ? <> 0
          AND n.edition=a.edition AND n.dataset='SpellName'
          AND n.record_id=CAST(json_extract(a.payload, '$.Spell') AS INTEGER)
        ORDER BY n.record_id
    """, (edition, 1 << (class_id - 1))).fetchall()
    return {
        "class": info,
        "specializations": [json.loads(spec[0]) for spec in specs],
        "legacy_trees": [json.loads(tree[0]) for tree in trees],
        "class_mask_spells": [dict(id=spell_id, name=name) for spell_id, name in skills],
    }


def rotation(db, edition, spec_id, apl):
    if edition == "retail" and spec_id is None:
        raise ValueError("正式服需要 --spec <专精ID>")
    if apl:
        row = db.execute(
            "SELECT path, revision, url, payload FROM rotation_reference WHERE edition=? AND path=?",
            (edition, apl),
        ).fetchone()
        if row is None:
            raise ValueError(f"找不到模拟优先级参考 {apl}")
        return dict(path=row[0], revision=row[1], url=row[2], reference=json.loads(row[3]))

    rows = db.execute("""
        SELECT s.record_id, CAST(json_extract(s.payload,'$.OrderIndex') AS INTEGER),
               CAST(json_extract(s.payload,'$.SpellID') AS INTEGER), n.label,
               (SELECT json_group_array(json(r.payload)) FROM record r
                WHERE r.edition=s.edition AND r.dataset='AssistedCombatRule'
                  AND CAST(json_extract(r.payload,'$.AssistedCombatStepID') AS INTEGER)=s.record_id)
        FROM record s JOIN record a ON a.edition=s.edition AND a.dataset='AssistedCombat'
          AND a.record_id=CAST(json_extract(s.payload,'$.AssistedCombatID') AS INTEGER)
        LEFT JOIN record n ON n.edition=s.edition AND n.dataset='SpellName'
          AND n.record_id=CAST(json_extract(s.payload,'$.SpellID') AS INTEGER)
        WHERE s.edition=? AND s.dataset='AssistedCombatStep'
          AND CAST(json_extract(a.payload,'$.ChrSpecializationID') AS INTEGER)=?
        ORDER BY 2
    """, (edition, spec_id)).fetchall()
    steps = [dict(id=id_, priority=priority, spell_id=spell_id, name=name,
                  rules=sorted(json.loads(rules), key=lambda rule: int(rule["OrderIndex"])))
             for id_, priority, spell_id, name, rules in rows]
    notes = db.execute("SELECT class_name,spec,summary,url FROM rotation_note WHERE edition=?",
                       (edition,)).fetchall()
    references = db.execute("SELECT path, revision, url FROM rotation_reference WHERE edition=? ORDER BY path",
                            (edition,)).fetchall()
    return dict(steps=steps, notes=notes, references=references)


def run(args) -> int:
    try:
        return _run(args)
    except (OSError, ValueError, sqlite3.Error, CaptureError) as exc:
        print(f"战斗模块：{exc}", file=sys.stderr)
        return 2


def _run(args) -> int:
    command = args.combat_command
    if command == "build-db":
        print(json.dumps(build_catalog(args.sources, args.db), ensure_ascii=False, indent=2))
    elif command == "coverage":
        print(json.dumps(coverage(args.db), ensure_ascii=False, indent=2))
    elif command == "lookup":
        with closing(open_catalog(args.db)) as db:
            records = db.execute(
                "SELECT payload FROM record WHERE edition=? AND dataset=? AND record_id=? ORDER BY line",
                (args.edition, args.table, args.id),
            ).fetchall()
        print(json.dumps([json.loads(row[0]) for row in records], ensure_ascii=False, indent=2))
        return 0 if records else 2
    elif command == "ability":
        with closing(open_catalog(args.db)) as db:
            result = ability(db, args.edition, args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result else 2
    elif command == "class":
        with closing(open_catalog(args.db)) as db:
            result = class_catalog(db, args.edition, args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif command == "rotation":
        with closing(open_catalog(args.db)) as db:
            result = rotation(db, args.edition, args.spec, args.apl)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif command == "talents":
        from .data.talents import export_addon_guides, retail_talents

        with closing(open_catalog(args.db)) as db:
            if args.export_addon:
                export_addon_guides(db, args.export_addon)
                print(args.export_addon)
            else:
                print(json.dumps(retail_talents(db, args.spec), ensure_ascii=False, indent=2))
    elif command in {"profile", "macro"}:
        profile = saved_profile(args.savedvars)
        if command == "profile":
            print(json.dumps(profile, ensure_ascii=False, indent=2))
        else:
            print(spell_macro(profile, args.spell))
    else:
        return _live(args)
    return 0


def _live(args):
    from .control.estop import EStop
    from .control.input import create_input
    from .control.input_helper import has_helper_socket, in_controller_app
    from .modules.combat import Fight, run_fight
    from .modules.quest import game_is_frontmost, wait_for_game

    cfg = load_config()
    cap = ScreenCapture(cfg.capture)
    region = combat_region(cfg.channel.x, cfg.channel.y)

    def read():
        return decode_combat(cap.grab(*region))

    if not 0 < args.duration <= 60:
        raise ValueError("--duration 必须在 0 到 60 秒之间")
    if args.combat_command == "scan":
        deadline = time.monotonic() + args.duration
        previous = None
        while time.monotonic() < deadline:
            sample = read()
            if sample:
                output = asdict(sample)
                del output["sequence"]
                output["edition"] = client_edition(sample.build)
                if output != previous:
                    print(json.dumps(output, ensure_ascii=False), flush=True)
                    previous = output
            time.sleep(.12)
        if previous is None:
            raise ValueError("未读取到战斗色条。游戏内执行 /wagent combat scan。")
        return 0

    if not 1 <= args.target <= 1048575:
        raise ValueError("目标 NPC ID 超出支持范围")
    if args.execute:
        if sys.platform != "darwin" or cfg.dry_run:
            raise ValueError("执行模式需要启用 macOS 实机输入")
        if in_controller_app():
            if not has_helper_socket():
                raise ValueError("请先打开 WowAgent Keyboard Mouse.app")
        else:
            from ApplicationServices import AXIsProcessTrusted

            if not AXIsProcessTrusted():
                raise ValueError("执行模式需要 macOS 辅助功能权限")
        wait_for_game()
    sample = read()
    if sample is None:
        raise ValueError("未读取到战斗色条")
    edition = client_edition(sample.build)
    if not edition or not coverage(args.db)[edition]["tables"].get("SpellName"):
        raise ValueError("当前客户端构建没有对应技能库")
    backend = create_input(cfg.input, force_mock=not args.execute)
    if args.execute and backend.name == "mock":
        raise ValueError("当前输入后端是 mock")
    stop = EStop(cfg.input.estop_hotkey)
    stop.bind(backend)
    if args.execute and not stop.start_hotkey():
        raise ValueError("急停热键启动失败")
    fight = Fight(args.target)
    try:
        status = run_fight(fight, read, backend, stop.check,
                           game_is_frontmost if args.execute else lambda: True, args.duration)
    finally:
        stop.stop()
    print(json.dumps({"status": status, "presses": fight.presses, "dry_run": not args.execute}))
    return 0 if status == "target_defeated" else 2


def add_parser(sub):
    parser = sub.add_parser("combat", help="战斗数据库、角色识别与单目标执行")
    commands = parser.add_subparsers(dest="combat_command", required=True)
    for name in ("build-db", "coverage", "lookup", "ability", "class", "rotation", "talents", "profile", "macro", "scan", "fight"):
        item = commands.add_parser(name)
        item.set_defaults(func=run)
        if name in {"build-db", "coverage", "lookup", "ability", "class", "rotation", "talents", "fight"}:
            item.add_argument("--db", type=Path, default=COMBAT_DB)
        if name == "build-db":
            item.add_argument("--sources", type=Path, default=SOURCE_DIR)
        if name in {"lookup", "ability", "class", "rotation"}:
            item.add_argument("--edition", choices=EDITIONS, required=True)
        if name == "lookup":
            item.add_argument("--table", required=True)
        if name in {"lookup", "ability", "class"}:
            item.add_argument("--id", type=int, required=True)
        if name == "rotation":
            selection = item.add_mutually_exclusive_group()
            selection.add_argument("--spec", type=int)
            selection.add_argument("--apl")
        if name == "talents":
            selection = item.add_mutually_exclusive_group()
            selection.add_argument("--spec", help="专精名称，例如 生存 或 Survival")
            selection.add_argument("--export-addon", type=Path, help="生成游戏内推荐数据")
        if name in {"profile", "macro"}:
            item.add_argument("--savedvars", type=Path, required=True)
        if name == "macro":
            item.add_argument("--spell", type=int, required=True)
        if name in {"scan", "fight"}:
            item.add_argument("--duration", type=float, default=12)
        if name == "fight":
            item.add_argument("--target", type=int, required=True)
            item.add_argument("--execute", action="store_true")
