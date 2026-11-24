"""数据桥接：把本项目拉取解密的消息增量写入 bot 库（新增，gateway 无此模块）。

数据流：wecom_core.pull_messages → data/wecom_messages.jsonl（本桥接的唯一输入）
      → bot 库 messages 表（gateway schema）。

- 游标存于 bot 库 watermark(key='bridge_seq')，按存档 seq 推进；
- 只桥接群消息（roomid 非空）——机器人只在群聊场景工作，单聊不进 bot 库；
- 撤回类消息（msgtype=revoke）标记 is_revoked=1 落库但不会被 matcher 吸收；
- 幂等：messages.msgid UNIQUE + INSERT OR IGNORE。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import Database, now_ms

log = logging.getLogger(__name__)

BRIDGE_CURSOR_KEY = "bridge_seq"

INSERT_MESSAGE = """
INSERT OR IGNORE INTO messages
    (msgid, roomid, conversation_key, from_userid, tolist, msgtype, msgtime, seq,
     content_json, text, is_revoked, ingested_at, parsed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
"""


def read_source_rows(output_jsonl: str, after_seq: int, limit: int = 1000) -> list[dict]:
    """从主 JSONL 读 seq > after_seq 的群消息行，按 seq 升序。"""
    path = Path(output_jsonl)
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = row.get("seq")
            if not isinstance(seq, int) or seq <= after_seq:
                continue
            if not row.get("message", {}).get("roomid"):
                continue
            rows.append(row)
    rows.sort(key=lambda r: r["seq"])
    return rows[:limit]


async def ingest_once(db: Database, output_jsonl: str) -> int:
    """桥接一批新消息，返回写入条数；游标只在实际写入后推进。"""
    row = db.query_one(
        "SELECT last_message_id FROM watermark WHERE key = ?", (BRIDGE_CURSOR_KEY,)
    )
    cursor = int(row["last_message_id"]) if row else 0

    pending = read_source_rows(output_jsonl, cursor)
    if not pending:
        return 0

    now = now_ms()
    highest = cursor
    inserted = 0
    for row in pending:
        seq = int(row["seq"])
        highest = max(highest, seq)
        msg = row.get("message", {}) or {}
        msgid = row.get("msgid")
        if not msgid:
            continue
        msgtype = str(msg.get("msgtype") or "unknown")
        result = await db.execute(
            INSERT_MESSAGE,
            (
                msgid,
                msg.get("roomid"),
                str(msg.get("roomid")),
                msg.get("from") or "",
                json.dumps(msg.get("tolist") or [], ensure_ascii=False),
                msgtype,
                int(msg.get("msgtime") or 0),
                seq,
                json.dumps(msg, ensure_ascii=False),
                1 if msgtype == "revoke" else 0,
                now,
            ),
        )
        inserted += result.rowcount if result.rowcount > 0 else 0

    if highest > cursor:
        await db.execute(
            """
            INSERT INTO watermark(key, last_message_id, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                updated_at = excluded.updated_at
            """,
            (BRIDGE_CURSOR_KEY, highest, now),
        )
    if inserted:
        log.info("桥接写入 %s 条群消息（seq 推进至 %s）", inserted, highest)
    return inserted
