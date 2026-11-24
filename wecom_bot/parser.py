"""消息解析（自 gateway/parser.py 原样移植）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

SELECT_PENDING = """
SELECT id, content_json FROM messages
WHERE parsed_at IS NULL
ORDER BY id
LIMIT ?
"""

UPDATE_PARSED = "UPDATE messages SET text = ?, parsed_at = ? WHERE id = ?"


def normalize_text(value: str) -> str:
    return " ".join(value.split())


@dataclass(frozen=True)
class ParsedContent:
    msgtype: str
    text: str | None
    media: dict[str, Any] | None


def parse_content(raw: dict[str, Any]) -> ParsedContent:
    msgtype = str(raw.get("msgtype") or "unknown")

    if msgtype == "text":
        body = raw.get("text")
        content = body.get("content") if isinstance(body, dict) else None
        text = normalize_text(content) if isinstance(content, str) and content.strip() else None
        return ParsedContent(msgtype=msgtype, text=text, media=None)

    body = raw.get(msgtype)
    media = body if isinstance(body, dict) else None
    return ParsedContent(msgtype=msgtype, text=None, media=media)


async def backfill(db, limit: int = 500, now: int | None = None) -> int:
    from .db import now_ms

    timestamp = now if now is not None else now_ms()
    rows = db.query(SELECT_PENDING, (limit,))
    processed = 0
    for row in rows:
        try:
            raw = json.loads(row["content_json"])
        except (TypeError, ValueError):
            log.warning("消息 %s 的 content_json 无法解析，跳过", row["id"])
            await db.execute(UPDATE_PARSED, (None, timestamp, row["id"]))
            processed += 1
            continue

        parsed = parse_content(raw if isinstance(raw, dict) else {})
        await db.execute(UPDATE_PARSED, (parsed.text, timestamp, row["id"]))
        processed += 1
    return processed
