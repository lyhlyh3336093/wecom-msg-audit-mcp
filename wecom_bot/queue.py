"""回执队列（自 gateway/queue.py 移植）。

移植差异：build_payload / enqueue 支持 mention_userid——群机器人文本消息
带 mentioned_list，实现回复时 @ 提问者（本场景的硬需求）。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = 20
WINDOW_MS = 60_000
MAX_ATTEMPTS = 5
RATE_LIMIT_ERRCODE = 45009
DEFAULT_FAILURE_TEXT = "抱歉，这条消息暂时没能处理，已经记录，稍后跟进。"


def build_payload(text: str, mention_userid: str | None = None) -> str:
    body: dict = {"msgtype": "text", "text": {"content": text}}
    if mention_userid:
        body["text"]["mentioned_list"] = [mention_userid]
    return json.dumps(body, ensure_ascii=False)


def resolve_webhook(db, roomid: str) -> int | None:
    row = db.query_one(
        """
        SELECT r.webhook_id AS webhook_id
        FROM rooms r
        WHERE r.roomid = ? AND r.enabled = 1 AND r.webhook_id IS NOT NULL
        """,
        (roomid,),
    )
    return int(row["webhook_id"]) if row else None


async def enqueue(
    db,
    *,
    invocation_id: int | None,
    roomid: str,
    text: str,
    now: int,
    mention_userid: str | None = None,
) -> int | None:
    webhook_id = resolve_webhook(db, roomid)
    if webhook_id is None:
        log.warning("群 %s 没有登记可用的 webhook，回执不入队：%s", roomid, text[:40])
        return None
    result = await db.execute(
        """
        INSERT INTO replies(invocation_id, webhook_id, payload, status, attempts, created_at)
        VALUES (?, ?, ?, 'pending', 0, ?)
        """,
        (invocation_id, webhook_id, build_payload(text, mention_userid), now),
    )
    return int(result.lastrowid or 0)


def sent_in_window(db, webhook_id: int, now: int, limit: int = RATE_LIMIT_PER_MINUTE) -> int:
    row = db.query_one(
        """
        SELECT COUNT(*) AS c FROM replies
        WHERE webhook_id = ? AND status = 'sent' AND sent_at IS NOT NULL AND sent_at > ?
        """,
        (webhook_id, now - WINDOW_MS),
    )
    return int(row["c"]) if row else 0


def rate_limited(db, webhook_id: int, now: int) -> bool:
    return sent_in_window(db, webhook_id, now) >= RATE_LIMIT_PER_MINUTE


async def cancel(db, reply_id: int) -> bool:
    result = await db.execute(
        "UPDATE replies SET status = 'canceled' WHERE id = ? AND status = 'pending'",
        (reply_id,),
    )
    return result.rowcount > 0
