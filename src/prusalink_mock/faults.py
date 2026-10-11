"""Fault rules: make the printer port misbehave on purpose.

A rule is ``{"match": {...}, "skip": N, "times": N|null, "action": {...}}``.
Rules are checked in the order they were added; the first one that matches
and still has hits left fires.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Optional

HANG_CAP_S = 30.0

MATCH_KEYS = ("method", "path", "path_glob", "auth")
AUTH_STATES = ("any", "none", "ok", "fail")
ACTIONS = {
    "status": ("status", "message", "body", "content_type", "close"),
    "malformed": ("mode", "keep_bytes", "body"),
    "delay": ("ms",),
    "hang": ("seconds",),
    "drop": ("when", "after_bytes"),
}
MALFORMED_MODES = ("truncate", "wrong_shape", "html")
DROP_WHEN = ("before_response", "mid_body")


class FaultError(ValueError):
    pass


@dataclass
class FaultRule:
    id: int
    match: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    skip: int = 0
    times: Optional[int] = None
    seen: int = 0
    hits: int = 0

    def describe(self) -> dict:
        return {"id": self.id, "match": self.match, "action": self.action, "skip": self.skip,
                "times": self.times, "seen": self.seen, "hits": self.hits}

    def matches(self, method: str, path: str, auth_state: str) -> bool:
        m = self.match
        if "method" in m and m["method"].upper() != method.upper():
            return False
        if "path" in m and m["path"] != path:
            return False
        if "path_glob" in m and not fnmatch.fnmatchcase(path, m["path_glob"]):
            return False
        if m.get("auth", "any") != "any" and m["auth"] != auth_state:
            return False
        return True


def _int(value: Any, name: str, lo: int, hi: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or (hi is not None and value > hi):
        raise FaultError(f"{name} must be an integer >= {lo}" + (f" and <= {hi}" if hi is not None else ""))
    return value


def parse_rule(doc: Any, rule_id: int) -> FaultRule:
    """Validate a rule document; raises FaultError with a readable message."""
    if not isinstance(doc, dict):
        raise FaultError("a fault rule must be a JSON object")
    for key in doc:
        if key not in ("match", "skip", "times", "action"):
            raise FaultError(f"unknown key {key!r}")
    match = doc.get("match", {})
    if not isinstance(match, dict):
        raise FaultError("match must be an object")
    for key, value in match.items():
        if key not in MATCH_KEYS:
            raise FaultError(f"unknown match key {key!r} (known: {', '.join(MATCH_KEYS)})")
        if not isinstance(value, str):
            raise FaultError(f"match.{key} must be a string")
    if match.get("auth", "any") not in AUTH_STATES:
        raise FaultError(f"match.auth must be one of {', '.join(AUTH_STATES)}")
    skip = _int(doc.get("skip", 0), "skip", 0)
    times = doc.get("times")
    if times is not None:
        times = _int(times, "times", 1)
    action = doc.get("action")
    if not isinstance(action, dict) or action.get("type") not in ACTIONS:
        raise FaultError(f"action.type must be one of {', '.join(ACTIONS)}")
    kind = action["type"]
    for key in action:
        if key != "type" and key not in ACTIONS[kind]:
            raise FaultError(f"unknown key {key!r} for a {kind} action")
    if kind == "status":
        _int(action.get("status"), "action.status", 100, 599)
        for key in ("message", "body", "content_type"):
            if key in action and not isinstance(action[key], str):
                raise FaultError(f"action.{key} must be a string")
        if "close" in action and not isinstance(action["close"], bool):
            raise FaultError("action.close must be true or false")
    elif kind == "malformed":
        if action.get("mode") not in MALFORMED_MODES:
            raise FaultError(f"action.mode must be one of {', '.join(MALFORMED_MODES)}")
        if "keep_bytes" in action:
            _int(action["keep_bytes"], "action.keep_bytes", 0)
    elif kind == "delay":
        _int(action.get("ms"), "action.ms", 0, 600000)
    elif kind == "hang":
        seconds = action.get("seconds", HANG_CAP_S)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= HANG_CAP_S:
            raise FaultError(f"action.seconds must be a number in 0..{HANG_CAP_S:g}")
    elif kind == "drop":
        if action.get("when") not in DROP_WHEN:
            raise FaultError(f"action.when must be one of {', '.join(DROP_WHEN)}")
        if "after_bytes" in action:
            _int(action["after_bytes"], "action.after_bytes", 0)
    return FaultRule(id=rule_id, match=dict(match), action=dict(action), skip=skip, times=times)


class FaultSet:
    def __init__(self) -> None:
        self.rules: list[FaultRule] = []
        self._next_id = 1

    def add(self, doc: Any) -> FaultRule:
        rule = parse_rule(doc, self._next_id)
        self._next_id += 1
        self.rules.append(rule)
        return rule

    def remove(self, rule_id: int) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        return len(self.rules) != before

    def clear(self) -> None:
        self.rules = []

    def pick(self, method: str, path: str, auth_state: str) -> Optional[FaultRule]:
        for rule in self.rules:
            if rule.times is not None and rule.hits >= rule.times:
                continue
            if not rule.matches(method, path, auth_state):
                continue
            rule.seen += 1
            if rule.seen <= rule.skip:
                continue
            rule.hits += 1
            return rule
        return None
