"""外部接口配置、请求模板渲染与外发策略（自 gateway/connectors.py 原样移植）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .redaction import RedactionRule, redact_value

PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
ORIGINAL_TEXT_KEYS = ("text", "message", "content", "merged_text")
VALID_POLICIES = ("raw", "redact", "params_only")


@dataclass(frozen=True)
class Connector:
    id: int
    name: str
    url: str
    method: str
    headers: dict[str, str]
    body_template: str
    egress_policy: str
    timeout_seconds: float
    retries: int


def render(template: str, context: dict[str, Any]) -> str:
    def substitute(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = context.get(key, "")
        return "" if value is None else str(value)

    return PLACEHOLDER.sub(substitute, template)


def strip_original_text(context: dict[str, Any]) -> dict[str, Any]:
    return {key: ("" if key in ORIGINAL_TEXT_KEYS else value) for key, value in context.items()}


def build_body(
    connector: Connector,
    context: dict[str, Any],
    extra_rules: list[RedactionRule] | None = None,
) -> str:
    if connector.egress_policy == "params_only":
        effective = strip_original_text(context)
    else:
        effective = context
    body = render(connector.body_template, effective)
    if connector.egress_policy != "redact":
        return body
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        from .redaction import redact_text

        return redact_text(body, extra_rules)
    return json.dumps(redact_value(parsed, extra_rules), ensure_ascii=False)


def load_connector(db, connector_id: int) -> Connector | None:
    row = db.query_one("SELECT * FROM connectors WHERE id = ?", (connector_id,))
    if row is None:
        return None
    try:
        headers = json.loads(row["headers"]) if row["headers"] else {}
    except (TypeError, ValueError):
        headers = {}
    policy = row["egress_policy"] if row["egress_policy"] in VALID_POLICIES else "redact"
    return Connector(
        id=int(row["id"]),
        name=row["name"],
        url=row["url"],
        method=(row["method"] or "POST").upper(),
        headers={str(k): str(v) for k, v in headers.items()},
        body_template=row["body_template"],
        egress_policy=policy,
        timeout_seconds=float(row["timeout_seconds"] or 10),
        retries=max(0, int(row["retries"] or 0)),
    )
