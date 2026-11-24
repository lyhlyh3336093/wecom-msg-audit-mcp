"""调用外部接口并落链路记录（自 gateway/invocation.py 移植）。

移植差异（本场景的核心扩展）：
1. 回执模板改为调用成功后渲染，上下文额外提供 ${response}（接口原始响应）
   与 ${answer}（从 JSON 响应中自动提取 answer 字段）——否则 RAG 的答案
   无法进入群回复。gateway 原实现是调用前渲染，回复里只能有静态文本。
2. 入回执队列时带 mention_userid（@ 提问者）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .connectors import Connector, build_body, load_connector, render
from .matcher import TriggerIntent
from .redaction import load_extra_rules

log = logging.getLogger(__name__)

RESPONSE_LIMIT = 4000
ANSWER_KEYS = ("answer", "reply", "content", "result", "data")

UPDATE_INVOCATION = """
UPDATE invocations
SET status = ?, request_redacted = ?, response = ?, errcode = ?, latency_ms = ?,
    error = ?, finished_at = ?
WHERE id = ?
"""


@dataclass
class DispatchResult:
    invocation_id: int
    status: str
    errcode: int | None = None
    response: str | None = None
    error: str | None = None
    latency_ms: int = 0
    reply_text: str | None = None
    attempts: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


def extract_answer(response_text: str | None) -> str:
    """从 JSON 响应提取 answer 字段；非 JSON 或无该字段时返回原文截断。"""
    if not response_text:
        return ""
    try:
        data = json.loads(response_text)
    except (TypeError, ValueError):
        return response_text[:RESPONSE_LIMIT]
    if isinstance(data, dict):
        for key in ANSWER_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value[:RESPONSE_LIMIT]
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ANSWER_KEYS:
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value[:RESPONSE_LIMIT]
        return json.dumps(data, ensure_ascii=False)[:RESPONSE_LIMIT]
    return response_text[:RESPONSE_LIMIT]


class Dispatcher:
    def __init__(
        self,
        runtime,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self._client = client
        self._backoff = sleep

    def _context(self, intent, connector: Connector | None, rule) -> dict[str, Any]:
        db = self.runtime.db
        room_name = db.query_one(
            "SELECT name FROM display_names WHERE kind = 'room' AND value = ?", (intent.roomid,)
        )
        user_name = db.query_one(
            "SELECT name FROM display_names WHERE kind = 'user' AND value = ?",
            (intent.from_userid,),
        )
        return {
            "roomid": intent.roomid,
            "room_name": room_name["name"] if room_name else intent.roomid,
            "userid": intent.from_userid,
            "user_name": user_name["name"] if user_name else intent.from_userid,
            "text": intent.text,
            "rule_name": rule["name"] if rule else "",
            "connector": connector.name if connector else "",
            "ts": intent.invocation_id,
        }

    def _reply_template(self, template_id: int | None) -> str | None:
        if template_id is None:
            return None
        row = self.runtime.db.query_one("SELECT body FROM templates WHERE id = ?", (template_id,))
        return row["body"] if row else None

    def _redaction_rules(self):
        row = self.runtime.db.query_one(
            "SELECT value FROM settings WHERE key = 'redaction_patterns'"
        )
        return load_extra_rules(row["value"] if row else None)

    async def dispatch(self, intent) -> DispatchResult:
        db = self.runtime.db
        row = db.query_one("SELECT * FROM invocations WHERE id = ?", (intent.invocation_id,))
        if row is None:
            return DispatchResult(intent.invocation_id, "failed", error="调用记录不存在")
        if row["status"] != "pending":
            return DispatchResult(
                intent.invocation_id, str(row["status"]), error="非 pending 状态，未发起请求"
            )

        connector = load_connector(db, intent.connector_id) if intent.connector_id else None
        if connector is None:
            await self._finish(intent.invocation_id, "failed", error="未配置外部接口")
            return DispatchResult(intent.invocation_id, "failed", error="未配置外部接口")

        rule = db.query_one("SELECT * FROM rules WHERE id = ?", (intent.rule_id,))
        context = self._context(intent, connector, rule)
        body = build_body(connector, context, self._redaction_rules())
        reply_template = self._reply_template(intent.template_id)

        started = time.monotonic()
        attempts = 0
        last_error: str | None = None
        errcode: int | None = None
        response_text: str | None = None

        while attempts <= connector.retries:
            attempts += 1
            try:
                response = await self._request(connector, body)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempts <= connector.retries:
                    await self._backoff(min(0.05 * attempts, 0.5))
                    continue
                break

            response_text = (response.text or "")[:RESPONSE_LIMIT]
            errcode = _extract_errcode(response_text)
            if response.status_code >= 500 and attempts <= connector.retries:
                last_error = f"HTTP {response.status_code}"
                await self._backoff(min(0.05 * attempts, 0.5))
                continue
            if 200 <= response.status_code < 300 and (errcode is None or errcode == 0):
                latency = int((time.monotonic() - started) * 1000)
                await self._finish(
                    intent.invocation_id,
                    "succeeded",
                    request=body,
                    response=response_text,
                    errcode=errcode,
                    latency=latency,
                    error=None,
                )
                return DispatchResult(
                    intent.invocation_id,
                    "succeeded",
                    errcode=errcode,
                    response=response_text,
                    latency_ms=latency,
                    reply_text=_render_reply(reply_template, context, response_text),
                    attempts=attempts,
                )
            last_error = f"HTTP {response.status_code} errcode={errcode}"
            break

        latency = int((time.monotonic() - started) * 1000)
        await self._finish(
            intent.invocation_id,
            "failed",
            request=body,
            response=response_text,
            errcode=errcode,
            latency=latency,
            error=last_error or "未知错误",
        )
        log.warning("调用 %s 失败：%s", connector.name, last_error)
        return DispatchResult(
            intent.invocation_id,
            "failed",
            errcode=errcode,
            response=response_text,
            error=last_error,
            latency_ms=latency,
            reply_text=None,
            attempts=attempts,
        )

    async def _request(self, connector: Connector, body: str) -> httpx.Response:
        client = self._client or httpx.AsyncClient()
        try:
            return await client.request(
                connector.method,
                connector.url,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8", **connector.headers},
                timeout=connector.timeout_seconds,
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def _finish(
        self,
        invocation_id: int,
        status: str,
        *,
        request: str | None = None,
        response: str | None = None,
        errcode: int | None = None,
        latency: int | None = None,
        error: str | None = None,
    ) -> None:
        from .db import now_ms

        await self.runtime.db.execute(
            UPDATE_INVOCATION,
            (status, request, response, errcode, latency, error, now_ms(), invocation_id),
        )

    async def cancel(self, invocation_id: int) -> bool:
        from .db import now_ms

        result = await self.runtime.db.execute(
            "UPDATE invocations SET status = 'canceled', finished_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_ms(), invocation_id),
        )
        return result.rowcount > 0

    PENDING_SQL = """
    SELECT i.id AS invocation_id, i.logical_msg_id, i.rule_id, i.connector_id,
           r.template_id, lm.roomid, lm.from_userid, lm.merged_text
    FROM invocations i
    JOIN logical_messages lm ON lm.id = i.logical_msg_id
    LEFT JOIN rules r ON r.id = i.rule_id
    WHERE i.status = 'pending'
    ORDER BY i.id
    LIMIT ?
    """

    def pending(self, limit: int = 50) -> list[TriggerIntent]:
        return [
            TriggerIntent(
                invocation_id=int(row["invocation_id"]),
                logical_message_id=int(row["logical_msg_id"]),
                roomid=row["roomid"],
                from_userid=row["from_userid"],
                text=row["merged_text"],
                rule_id=int(row["rule_id"]),
                connector_id=row["connector_id"],
                template_id=row["template_id"],
            )
            for row in self.runtime.db.query(self.PENDING_SQL, (limit,))
        ]

    async def process_once(self) -> int:
        processed = 0
        for intent in self.pending():
            result = await self.dispatch(intent)
            await self._enqueue_reply(intent, result)
            processed += 1
        return processed

    async def _enqueue_reply(self, intent, result: DispatchResult) -> None:
        if result.status in {"canceled", "skipped"}:
            return
        text = result.reply_text if result.succeeded else self._failure_text()
        if not text:
            return
        from .queue import enqueue

        await enqueue(
            self.runtime.db,
            invocation_id=result.invocation_id,
            roomid=intent.roomid,
            text=text,
            now=_now(),
            mention_userid=intent.from_userid,
        )

    def _failure_text(self) -> str | None:
        from .queue import DEFAULT_FAILURE_TEXT

        row = self.runtime.db.query_one(
            "SELECT value FROM settings WHERE key = 'failure_reply_text'"
        )
        if row is not None:
            return str(row["value"])
        return DEFAULT_FAILURE_TEXT

    async def run(self) -> None:
        runtime = self.runtime
        while not runtime.stop.is_set():
            try:
                await self.process_once()
            except Exception:
                log.exception("调用循环异常")
            await self._sleep(1.0)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.runtime.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _render_reply(
    template: str | None, context: dict[str, Any], response_text: str | None
) -> str | None:
    """调用成功后渲染回执：上下文注入 ${response} 与 ${answer}。"""
    if not template:
        return None
    enriched = {**context, "response": response_text or "", "answer": extract_answer(response_text)}
    return render(template, enriched)


def _now() -> int:
    from .db import now_ms

    return now_ms()


def _extract_errcode(text: str) -> int | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        value = data.get("errcode")
        if isinstance(value, int):
            return value
    return None
