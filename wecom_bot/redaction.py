"""脱敏（自 gateway/redaction.py 原样移植）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PHONE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
ID_CARD = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule("phone", PHONE, r"\1****\3"),
    RedactionRule("id_card", ID_CARD, r"\1********\3"),
)


def load_extra_rules(raw: str | None) -> list[RedactionRule]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    rules: list[RedactionRule] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        pattern = item.get("pattern")
        replacement = str(item.get("replacement") or "")
        if not name or not pattern:
            continue
        try:
            rules.append(RedactionRule(name, re.compile(str(pattern)), replacement))
        except re.error:
            continue
    return rules


def redact_text(value: str, extra: list[RedactionRule] | None = None) -> str:
    result = value
    for rule in (*DEFAULT_RULES, *(extra or [])):
        result = rule.pattern.sub(rule.replacement, result)
    return result


def redact_value(value: Any, extra: list[RedactionRule] | None = None) -> Any:
    if isinstance(value, str):
        return redact_text(value, extra)
    if isinstance(value, list):
        return [redact_value(item, extra) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, extra) for key, item in value.items()}
    return value
