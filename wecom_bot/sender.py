"""回执投递（自 gateway/sender.py 原样移植）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import httpx

from .db import now_ms
from .queue import (
    MAX_ATTEMPTS,
    RATE_LIMIT_ERRCODE,
    RATE_LIMIT_PER_MINUTE,
    sent_in_window,
)

log = logging.getLogger(__name__)

PENDING_SQL = """
SELECT id, webhook_id, payload, attempts FROM replies
WHERE status = 'pending'
ORDER BY id
LIMIT ?
"""


class Sender:
    def __init__(
        self,
        runtime,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = now_ms,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self._client = client
        self.clock = clock
        self._backoff = sleep

    async def _post(self, url: str, payload: str) -> httpx.Response:
        client = self._client or httpx.AsyncClient()
        try:
            return await client.post(
                url,
                content=payload.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=10.0,
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def _update(
        self,
        reply_id: int,
        *,
        status: str,
        attempts: int,
        errcode: int | None = None,
        error: str | None = None,
        now: int,
    ) -> None:
        sent_at = now if status == "sent" else None
        await self.runtime.db.execute(
            """
            UPDATE replies
            SET status = ?, attempts = ?, errcode = ?, error = ?, sent_at = ?
            WHERE id = ?
            """,
            (status, attempts, errcode, error, sent_at, reply_id),
        )

    async def send_one(self, reply, now: int) -> str:
        db = self.runtime.db
        webhook = db.query_one("SELECT url FROM webhooks WHERE id = ?", (reply["webhook_id"],))
        attempts = int(reply["attempts"]) + 1

        if webhook is None:
            await self._update(
                int(reply["id"]),
                status="failed",
                attempts=attempts,
                error="webhook 不存在",
                now=now,
            )
            return "failed"

        try:
            response = await self._post(webhook["url"], reply["payload"])
        except httpx.HTTPError as exc:
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            await self._update(
                int(reply["id"]),
                status=status,
                attempts=attempts,
                error=f"{type(exc).__name__}: {exc}",
                now=now,
            )
            return status

        errcode = _errcode_of(response.text)
        if 200 <= response.status_code < 300 and errcode == 0:
            await self._update(int(reply["id"]), status="sent", attempts=attempts, now=now)
            return "sent"

        if errcode == RATE_LIMIT_ERRCODE:
            await self._update(
                int(reply["id"]),
                status="pending",
                attempts=attempts,
                errcode=errcode,
                error="被服务端限频（45009），等待窗口恢复",
                now=now,
            )
            log.warning("webhook %s 被限频，回执 %s 退回队列", reply["webhook_id"], reply["id"])
            return "pending"

        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        await self._update(
            int(reply["id"]),
            status=status,
            attempts=attempts,
            errcode=errcode,
            error=f"HTTP {response.status_code} errcode={errcode}",
            now=now,
        )
        return status

    def pending(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.runtime.db.query(PENDING_SQL, (limit,))]

    async def drain_once(self) -> int:
        now = self.clock()
        used: dict[int, int] = {}
        sent = 0
        for reply in self.pending():
            webhook_id = reply["webhook_id"]
            if webhook_id is None:
                await self._update(int(reply["id"]), status="failed", attempts=1,
                                   error="缺少 webhook", now=now)
                continue
            current = used.get(int(webhook_id))
            if current is None:
                current = sent_in_window(self.runtime.db, int(webhook_id), now)
            if current >= RATE_LIMIT_PER_MINUTE:
                continue
            used[int(webhook_id)] = current + 1
            if await self.send_one(reply, now) == "sent":
                sent += 1
        return sent

    async def run(self) -> None:
        runtime = self.runtime
        while not runtime.stop.is_set():
            try:
                await self.drain_once()
            except Exception:
                log.exception("回执循环异常")
            try:
                await asyncio.wait_for(runtime.stop.wait(), timeout=1.0)
            except TimeoutError:
                pass


def _errcode_of(text: str) -> int | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        value = data.get("errcode")
        if isinstance(value, int):
            return value
        if "errcode" not in data:
            return 0
    return None
