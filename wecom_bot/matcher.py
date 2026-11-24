"""匹配引擎（自 gateway/matcher.py 移植）。

移植差异：absorb() 增加群范围过滤——只吸收 rooms 表中登记且 enabled 的群
（gateway 在采集侧用 whitelist 过滤，本项目采集器无白名单概念）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .db import now_ms

log = logging.getLogger(__name__)

BOT_TEXT_ECHO_WINDOW_MS = 2 * 60 * 1000

INSERT_WINDOW = """
INSERT INTO logical_messages
    (roomid, from_userid, merged_text, msg_ids, window_start, window_end,
     sealed, matched_at, created_at)
VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
"""

APPEND_WINDOW = """
UPDATE logical_messages
SET merged_text = ?, msg_ids = ?, window_end = ?
WHERE id = ?
"""

SEAL_BY_GAP = "UPDATE logical_messages SET sealed = 1 WHERE id = ?"
SEAL_EXPIRED = """
UPDATE logical_messages
SET sealed = 1
WHERE sealed = 0 AND window_end + ? < ?
"""
MARK_MATCHED = "UPDATE logical_messages SET matched_at = ? WHERE id = ?"

CLAIM_INVOCATION = """
INSERT OR IGNORE INTO invocations
    (logical_msg_id, rule_id, connector_id, status, created_at)
VALUES (?, ?, ?, ?, ?)
"""

ABSORB_SQL = """
SELECT * FROM messages
WHERE id > ? AND roomid IS NOT NULL AND roomid <> ''
  AND msgtype = 'text' AND is_revoked = 0
  AND roomid IN (SELECT roomid FROM rooms WHERE enabled = 1)
ORDER BY id LIMIT 500
"""


@dataclass
class TriggerIntent:
    invocation_id: int
    logical_message_id: int
    roomid: str
    from_userid: str
    text: str
    rule_id: int
    connector_id: int | None
    template_id: int | None


def parse_keywords(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


class Matcher:
    def __init__(self, runtime, clock: Callable[[], int] = now_ms) -> None:
        self.runtime = runtime
        self.clock = clock

    def _trigger_enabled(self) -> bool:
        row = self.runtime.db.query_one("SELECT value FROM settings WHERE key = 'trigger_enabled'")
        if row is None:
            return True
        return str(row["value"]).lower() not in {"0", "false", "off", "disabled"}

    def _is_blacklisted(self, userid: str) -> bool:
        row = self.runtime.db.query_one(
            "SELECT 1 AS hit FROM blacklist WHERE userid = ?", (userid,)
        )
        return row is not None

    def _is_bot(self, userid: str) -> bool:
        return userid in self.runtime.config.bot_self_userids

    def _is_recent_reply_echo(self, text: str, now: int) -> bool:
        rows = self.runtime.db.query(
            "SELECT payload FROM replies WHERE sent_at IS NOT NULL AND sent_at > ? LIMIT 50",
            (now - BOT_TEXT_ECHO_WINDOW_MS,),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            body = payload.get("text", {}) if isinstance(payload, dict) else {}
            if isinstance(body, dict) and body.get("content") == text:
                return True
        return False

    def _watermark(self) -> int:
        row = self.runtime.db.query_one(
            "SELECT last_message_id FROM watermark WHERE key = 'matcher'"
        )
        return int(row["last_message_id"]) if row else 0

    async def _set_watermark(self, value: int, now: int) -> None:
        await self.runtime.db.execute(
            """
            INSERT INTO watermark(key, last_message_id, updated_at) VALUES('matcher', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                updated_at = excluded.updated_at
            """,
            (value, now),
        )

    async def _open_window(self, roomid: str, userid: str) -> dict[str, Any] | None:
        row = self.runtime.db.query_one(
            """
            SELECT * FROM logical_messages
            WHERE roomid = ? AND from_userid = ? AND sealed = 0
            ORDER BY id DESC LIMIT 1
            """,
            (roomid, userid),
        )
        return dict(row) if row else None

    async def absorb(self) -> int:
        now = self.clock()
        window_ms = self.runtime.config.merge_window_seconds * 1000
        cursor = self._watermark()
        rows = self.runtime.db.query(ABSORB_SQL, (cursor,))

        highest = cursor
        for row in rows:
            highest = max(highest, int(row["id"]))
            if self._is_bot(row["from_userid"]):
                continue
            text = row["text"]
            if not text:
                continue
            if self._is_recent_reply_echo(text, now):
                continue

            opened = await self._open_window(row["roomid"], row["from_userid"])
            if opened and int(row["msgtime"]) - int(opened["window_end"]) <= window_ms:
                msg_ids = json.loads(opened["msg_ids"])
                msg_ids.append(int(row["id"]))
                await self.runtime.db.execute(
                    APPEND_WINDOW,
                    (
                        f"{opened['merged_text']} {text}".strip(),
                        json.dumps(msg_ids),
                        int(row["msgtime"]),
                        int(opened["id"]),
                    ),
                )
                continue

            if opened:
                await self.runtime.db.execute(SEAL_BY_GAP, (int(opened["id"]),))
            await self.runtime.db.execute(
                INSERT_WINDOW,
                (
                    row["roomid"],
                    row["from_userid"],
                    text,
                    json.dumps([int(row["id"])]),
                    int(row["msgtime"]),
                    int(row["msgtime"]),
                    now,
                ),
            )

        await self._set_watermark(highest, now)
        return len(rows)

    async def seal_expired(self) -> list[dict[str, Any]]:
        now = self.clock()
        window_ms = self.runtime.config.merge_window_seconds * 1000
        await self.runtime.db.execute(SEAL_EXPIRED, (window_ms, now))
        rows = self.runtime.db.query(
            "SELECT * FROM logical_messages WHERE sealed = 1 AND matched_at IS NULL ORDER BY id"
        )
        return [dict(row) for row in rows]

    def _matching_rules(self, text: str) -> list[dict[str, Any]]:
        haystack = text.casefold()
        rules = [
            dict(row)
            for row in self.runtime.db.query(
                "SELECT * FROM rules WHERE enabled = 1 ORDER BY priority ASC, id ASC"
            )
        ]
        return [
            rule
            for rule in rules
            if any(keyword.casefold() in haystack for keyword in parse_keywords(rule["keywords"]))
        ]

    async def _claim(self, logical_id: int, rule_id: int, connector_id: int | None, now: int):
        result = await self.runtime.db.execute(
            CLAIM_INVOCATION, (logical_id, rule_id, connector_id, "pending", now)
        )
        if result.rowcount == 0:
            return None
        return int(result.lastrowid or 0)

    async def _clear_cooldowns(self, userid: str) -> None:
        await self.runtime.db.execute("DELETE FROM cooldowns WHERE userid = ?", (userid,))

    async def tick(self) -> list[TriggerIntent]:
        now = self.clock()
        await self.absorb()
        windows = await self.seal_expired()
        intents: list[TriggerIntent] = []
        for window in windows:
            intents.extend(await self._process(window, now))
        return intents

    async def _process(self, window: dict[str, Any], now: int) -> list[TriggerIntent]:
        userid = window["from_userid"]
        text = window["merged_text"]
        logical_id = int(window["id"])

        if not self._trigger_enabled() or self._is_blacklisted(userid):
            await self.runtime.db.execute(MARK_MATCHED, (now, logical_id))
            return []

        matched = self._matching_rules(text)
        if not matched:
            await self._clear_cooldowns(userid)
            await self.runtime.db.execute(MARK_MATCHED, (now, logical_id))
            return []

        primary, *others = matched
        for rule in others:
            await self._claim(logical_id, int(rule["id"]), rule["connector_id"], now)
            await self.runtime.db.execute(
                "UPDATE invocations SET status = 'skipped' "
                "WHERE logical_msg_id = ? AND rule_id = ? AND status = 'pending'",
                (logical_id, int(rule["id"])),
            )

        rule_id = int(primary["id"])
        cooldown = int(primary["cooldown_seconds"] or 0)
        if cooldown > 0:
            row = self.runtime.db.query_one(
                "SELECT last_fired_at FROM cooldowns WHERE userid = ? AND rule_id = ?",
                (userid, rule_id),
            )
            if row is not None and now - int(row["last_fired_at"]) < cooldown * 1000:
                await self.runtime.db.execute(MARK_MATCHED, (now, logical_id))
                return []

        invocation_id = await self._claim(logical_id, rule_id, primary["connector_id"], now)
        await self.runtime.db.execute(MARK_MATCHED, (now, logical_id))
        if invocation_id is None:
            return []

        await self.runtime.db.execute(
            """
            INSERT INTO cooldowns(userid, rule_id, last_fired_at) VALUES (?, ?, ?)
            ON CONFLICT(userid, rule_id) DO UPDATE SET last_fired_at = excluded.last_fired_at
            """,
            (userid, rule_id, now),
        )
        return [
            TriggerIntent(
                invocation_id=invocation_id,
                logical_message_id=logical_id,
                roomid=window["roomid"],
                from_userid=userid,
                text=text,
                rule_id=rule_id,
                connector_id=primary["connector_id"],
                template_id=primary["template_id"],
            )
        ]

    async def run(self) -> None:
        runtime = self.runtime
        while not runtime.stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("匹配循环异常")
            await self._sleep(runtime.config.flush_interval_seconds)

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        try:
            await asyncio.wait_for(self.runtime.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
