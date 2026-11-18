"""Parse WoW SavedVariables Lua tables (Journalator logs) into Python objects.

Generic parser for the Lua table subset used in SavedVariables files:
nested tables, string keys with `name =`, bracket keys `[123] =`, quoted
strings with escapes, numbers, booleans, nil, array-part entries.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--\[\[.*?\]\]|--[^\n]*) |
    (?P<brace_open>\{) |
    (?P<brace_close>\}) |
    (?P<comma>,) |
    (?P<eq>=) |
    (?P<lbracket>\[) |
    (?P<rbracket>\]) |
    (?P<number>-?\d+\.?\d*(?:[eE][+-]?\d+)?) |
    (?P<bool>true|false) |
    (?P<nil>nil) |
    (?P<name>[A-Za-z_][A-Za-z0-9_]*) |
    (?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    """,
    re.VERBOSE | re.DOTALL,
)


class LuaParseError(ValueError):
    pass


def tokenize(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            raise LuaParseError(f"token error at offset {pos}: {text[pos:pos+30]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        out.append((kind, m.group(kind)))
    return out


def _unescape(s: str) -> str:
    body = s[1:-1]
    return (
        body.replace("\\\\", "\x00")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\x00", "\\")
    )


class _Stream:
    def __init__(self, toks: List[Tuple[str, str]]) -> None:
        self.toks = toks
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> Optional[Tuple[str, str]]:
        t = self.peek()
        if t is not None:
            self.i += 1
        return t

    def expect(self, kind: str) -> Tuple[str, str]:
        t = self.next()
        if t is None or t[0] != kind:
            raise LuaParseError(f"expected {kind}, got {t}")
        return t


def parse_lua_table(text: str) -> Dict[str, Any]:
    """Parse the `Name = { ... }` entries of a SavedVariables file body."""
    s = _Stream(tokenize(text))
    out: Dict[str, Any] = {}

    def parse_value():
        t = s.peek()
        if t is None:
            raise LuaParseError("unexpected EOF in value")
        kind, val = t
        if kind == "brace_open":
            s.next()  # consume '{' before recursing
            return parse_table()
        if kind == "string":
            s.next()
            return _unescape(val)
        if kind == "number":
            s.next()
            try:
                return int(val)
            except ValueError:
                return float(val)
        if kind == "bool":
            s.next()
            return val == "true"
        if kind == "nil":
            s.next()
            return None
        raise LuaParseError(f"unexpected value token {kind}: {val!r}")

    def parse_key():
        # key already peeked as name or '[' consumed by caller
        raise AssertionError("internal")

    def parse_table():
        table: Dict[Any, Any] = {}
        array: List[Any] = []
        while True:
            t = s.peek()
            if t is None:
                raise LuaParseError("unexpected EOF in table")
            kind, val = t
            if kind == "brace_close":
                s.next()
                break
            if kind == "comma":
                s.next()
                continue
            # keyed or bare entry
            t2 = s.toks[s.i + 1] if s.i + 1 < len(s.toks) else None
            if kind == "name" and t2 and t2[0] == "eq":
                s.next()
                s.next()
                table[val] = parse_value()
            elif kind == "lbracket":
                s.next()
                kt = s.expect("number") if (s.peek() and s.peek()[0] == "number") else s.expect("string")
                s.expect("rbracket")
                s.expect("eq")
                key: Any = int(kt[1]) if kt[0] == "number" else _unescape(kt[1])
                table[key] = parse_value()
            else:
                array.append(parse_value())
        if array and not table:
            return array
        if array:
            for idx, v in enumerate(array, start=1):
                table.setdefault(idx, v)
        return table

    while True:
        t = s.peek()
        if t is None:
            break
        kind, val = t
        if kind == "name":
            s.next()
            t2 = s.peek()
            if t2 and t2[0] == "eq":
                s.next()
                out[val] = parse_value()
        else:
            s.next()  # skip stray tokens
    return out


def journalator_sales(parsed: Dict[str, Any], limit: int = 500) -> List[dict]:
    """Best-effort extraction of sale rows from a Journalator SavedVariables
    structure. Layouts vary by version; we look for any list whose entries
    carry price/quantity-ish fields and return [] rather than guess wrong."""

    rows: List[dict] = []

    def row_of(item: dict) -> Optional[dict]:
        keys = {str(k).lower() for k in item.keys()}
        if "price" in keys and ("quantity" in keys or "count" in keys or "stacksize" in keys):
            return {
                "price": item.get("price") or item.get("Price"),
                "quantity": item.get("quantity") or item.get("count") or item.get("stackSize") or 1,
                "name": item.get("name") or item.get("itemName") or item.get("itemLink"),
                "ts": item.get("timestamp") or item.get("time") or item.get("rawDay"),
            }
        return None

    def visit(node: Any, depth: int) -> None:
        if depth > 7 or len(rows) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                if len(rows) >= limit:
                    return
                if isinstance(item, dict):
                    r = row_of(item)
                    if r:
                        rows.append(r)
                    else:
                        visit(item, depth + 1)
                elif isinstance(item, list):
                    visit(item, depth + 1)
        elif isinstance(node, dict):
            for v in node.values():
                if isinstance(v, dict):
                    r = row_of(v)
                    if r:
                        if len(rows) < limit:
                            rows.append(r)
                        continue
                if isinstance(v, (list, dict)):
                    visit(v, depth + 1)

    visit(parsed, 0)
    return rows
