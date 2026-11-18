"""Markdown session/income report from the SQLite database."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..core.paths import REPORT_DIR
from .db import Database


def _fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def build_report(db: Database, since_hours: float = 48.0) -> str:
    since = time.time() - since_hours * 3600
    lines: list[str] = []
    lines.append(f"# WowAgent 运行报告（近 {since_hours:.0f} 小时）\n")

    sessions = [s for s in db.list_sessions(100) if s["started_at"] >= since]
    lines.append("## 会话\n")
    if not sessions:
        lines.append("- 无记录\n")
    for s in sessions:
        dur = (s["ended_at"] or time.time()) - s["started_at"]
        lines.append(
            f"- {_fmt_ts(s['started_at'])} {s['module']:<8} {dur/60:6.1f} min  {s['status']}"
        )

    gold = [g for g in db.gold_series(since)]
    lines.append("\n## 金币\n")
    if len(gold) >= 2:
        first, last = gold[0], gold[-1]
        delta = last["gold"] - first["gold"]
        hours = max(1e-6, (last["ts"] - first["ts"]) / 3600.0)
        lines.append(f"- 起点 {first['gold']} G（{_fmt_ts(first['ts'])}）")
        lines.append(f"- 最新 {last['gold']} G（{_fmt_ts(last['ts'])}）")
        lines.append(f"- 变化 **{delta:+d} G**，折合 **{delta / hours:+.0f} G/小时**")
    else:
        lines.append("- 金币样本不足（通道 SLOWROT 金币消息约每 24 s 一条）")

    counts = db.event_counts(since)
    lines.append("\n## 事件统计\n")
    if counts:
        for kind, c in counts.items():
            lines.append(f"- {kind}: {c}")
    else:
        lines.append("- 无")

    err = db.recent_events(since, 500)
    problems = [e for e in err if "stop" in e["kind"] or "crash" in e["kind"] or "stuck" in e["kind"]]
    if problems:
        lines.append("\n## 最近异常\n")
        for e in problems[:15]:
            lines.append(f"- {_fmt_ts(e['ts'])} {e['kind']} {e['data'] or ''}")
    return "\n".join(lines) + "\n"


def save_report(db: Database, since_hours: float = 48.0, path: Optional[Path] = None) -> Path:
    path = Path(path) if path else REPORT_DIR / f"{time.strftime('%Y%m%d_%H%M')}_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(db, since_hours), encoding="utf-8")
    return path
