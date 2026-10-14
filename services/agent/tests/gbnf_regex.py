"""Test-only: translate the (non-recursive) GBNF subset our compiler emits into one Python regex, so the grammar can be
exercised without llama.cpp. Handles rule references (inlined), "literals", [classes], groups, |, ? * + and {m,n}."""
from __future__ import annotations

import re

_IDENT = re.compile(r"[a-z][a-z0-9_-]*")


def parse_rules(gbnf: str) -> dict[str, str]:
    rules = {}
    for line in gbnf.splitlines():
        if line.strip():
            name, body = line.split(" ::= ", 1)
            rules[name.strip()] = body
    return rules


def to_regex(gbnf: str, start: str = "root") -> "re.Pattern[str]":
    rules = parse_rules(gbnf)
    cache: dict[str, str] = {}

    def expand(name: str, stack: tuple = ()) -> str:
        if name in stack:
            raise ValueError(f"recursive rule {name}")
        if name not in cache:
            cache[name] = "(?:" + body(rules[name], stack + (name,)) + ")"
        return cache[name]

    def body(s: str, stack: tuple) -> str:
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c == " ":
                i += 1
            elif c == '"':
                j, lit = i + 1, []
                while s[j] != '"':
                    if s[j] == "\\":
                        j += 1
                        lit.append({"n": "\n", "t": "\t"}.get(s[j], s[j]))
                    else:
                        lit.append(s[j])
                    j += 1
                out.append(re.escape("".join(lit)))
                i = j + 1
            elif c == "[":
                j = i + 1
                while s[j] != "]" or s[j - 1] == "\\" and s[j - 2] != "\\":
                    j += 1
                out.append(s[i:j + 1])
                i = j + 1
            elif c in "()|?*+":
                out.append({"(": "(?:"}.get(c, c))
                i += 1
            elif c == "{":
                j = s.index("}", i)
                out.append(s[i:j + 1])
                i = j + 1
            else:
                m = _IDENT.match(s, i)
                if not m:
                    raise ValueError(f"cannot parse GBNF near {s[i:i + 20]!r}")
                out.append(expand(m.group(0), stack))
                i = m.end()
        return "".join(out)

    return re.compile(expand(start), re.S)
