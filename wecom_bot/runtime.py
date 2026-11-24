"""Bot 运行时：装配数据库/循环/缺省种子，并管理启停。"""

from __future__ import annotations

import asyncio
import json
import logging

from .bridge import ingest_once
from .config import BotConfig
from .db import Database, now_ms
from .invocation import Dispatcher
from .matcher import Matcher
from .parser import backfill
from .queue import DEFAULT_FAILURE_TEXT

log = logging.getLogger(__name__)


class BotRuntime:
    """进程内共享状态：配置、bot 数据库、停机信号与长驻循环。"""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db
        self.stop = asyncio.Event()

    async def start_loops(self, output_jsonl: str) -> list[asyncio.Task]:
        matcher = Matcher(self)
        dispatcher = Dispatcher(self)
        sender = _SenderAdapter(self)
        tasks = [
            asyncio.create_task(_ingest_loop(self, output_jsonl), name="bot-ingest"),
            asyncio.create_task(_parse_loop(self), name="bot-parse"),
            asyncio.create_task(matcher.run(), name="bot-matcher"),
            asyncio.create_task(dispatcher.run(), name="bot-dispatcher"),
            asyncio.create_task(sender.run(), name="bot-sender"),
        ]
        if self.config.aibot_bot_id and self.config.aibot_secret:
            from .aibot import run_aibot

            tasks.append(asyncio.create_task(run_aibot(self), name="bot-aibot"))
        return tasks

    async def shutdown(self) -> None:
        self.stop.set()


class _SenderAdapter:
    """sender.Sender 需要的对象形态；直接组合避免继承耦合。"""

    def __init__(self, runtime: BotRuntime) -> None:
        from .sender import Sender

        self._sender = Sender(runtime)

    def __getattr__(self, name):
        return getattr(self._sender, name)


async def _ingest_loop(runtime: BotRuntime, output_jsonl: str) -> None:
    while not runtime.stop.is_set():
        try:
            await ingest_once(runtime.db, output_jsonl)
        except Exception:
            log.exception("桥接循环异常")
        try:
            await asyncio.wait_for(runtime.stop.wait(), timeout=runtime.config.ingest_interval_seconds)
        except TimeoutError:
            pass


async def _parse_loop(runtime: BotRuntime) -> None:
    while not runtime.stop.is_set():
        try:
            await backfill(runtime.db)
        except Exception:
            log.exception("解析循环异常")
        try:
            await asyncio.wait_for(runtime.stop.wait(), timeout=runtime.config.flush_interval_seconds)
        except TimeoutError:
            pass


def seed_defaults(db: Database, config: BotConfig) -> None:
    """首启种子：缺省规则/connector/模板/失败文案。已存在的行不覆盖。"""
    now = now_ms()

    if not db.query_one("SELECT 1 FROM settings WHERE key = 'failure_reply_text'"):
        db.execute_now(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('failure_reply_text', ?)",
            (DEFAULT_FAILURE_TEXT,),
        )

    connector_id: int | None = None
    if config.rag_api_url and not db.query_one("SELECT 1 FROM connectors WHERE name = 'rag'"):
        db.execute_now(
            """
            INSERT INTO connectors(name, url, method, headers, body_template,
                                   egress_policy, timeout_seconds, retries, created_at)
            VALUES ('rag', ?, 'POST', ?, '{"question": "${text}"}',
                    'raw', 20, 2, ?)
            """,
            (
                config.rag_api_url,
                json_headers(config.rag_api_token),
                now,
            ),
        )
        row = db.query_one("SELECT id FROM connectors WHERE name = 'rag'")
        connector_id = int(row["id"]) if row else None

    template_id: int | None = None
    if not db.query_one("SELECT 1 FROM templates WHERE name = 'rag_reply'"):
        db.execute_now(
            "INSERT INTO templates(name, body, channel, created_at) VALUES ('rag_reply', ?, 'group', ?)",
            (config.rag_reply_template, now),
        )
        row = db.query_one("SELECT id FROM templates WHERE name = 'rag_reply'")
        template_id = int(row["id"]) if row else None

    keywords = json.dumps(list(config.mention_keywords), ensure_ascii=False)
    if not db.query_one("SELECT 1 FROM rules WHERE name = '智能问答'"):
        db.execute_now(
            """
            INSERT INTO rules(name, priority, enabled, keywords, connector_id,
                              template_id, cooldown_seconds, created_at)
            VALUES ('智能问答', 100, 1, ?, ?, ?, 60, ?)
            """,
            (keywords, connector_id, template_id, now),
        )
    elif connector_id is not None and template_id is not None:
        db.execute_now(
            "UPDATE rules SET connector_id = ?, template_id = ? WHERE name = '智能问答' "
            "AND connector_id IS NULL",
            (connector_id, template_id),
        )


def json_headers(token: str) -> str:
    import json

    headers = {}
    if token:
        headers["X-API-Key"] = token
    return json.dumps(headers)
