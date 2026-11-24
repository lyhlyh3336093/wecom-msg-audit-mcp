"""企微智能机器人长连接通道（wss://openws.work.weixin.qq.com）。

独立于「存档 → matcher → webhook」链路：群聊/单聊里 @机器人 的消息由企微
云端经 WebSocket 长连接推送过来（官方 aibot 协议，Bot ID + Secret 认证），
处理完直接流式回复，无需公网回调 URL，也无需群机器人 Webhook。

依赖：wecom-aibot-sdk（lazy import，未配置 BOT_AIBOT_ID 时不加载）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .connectors import render
from .invocation import extract_answer

log = logging.getLogger(__name__)

RAG_TIMEOUT_SECONDS = 120.0


def _failure_text(db) -> str:
    from .queue import DEFAULT_FAILURE_TEXT

    row = db.query_one("SELECT value FROM settings WHERE key = 'failure_reply_text'")
    return str(row["value"]) if row else DEFAULT_FAILURE_TEXT


def frame_text(frame: dict[str, Any]) -> str:
    body = frame.get("body") or {}
    text_part = body.get("text") or {}
    return str(text_part.get("content") or "").strip()


def frame_userid(frame: dict[str, Any]) -> str:
    body = frame.get("body") or {}
    from_info = body.get("from") or {}
    return str(from_info.get("userid") or "")


def compose_reply(template: str, user_name: str, answer: str) -> str:
    return render(template, {"user_name": user_name, "userid": user_name, "answer": answer})


async def query_rag(
    config, question: str, client: httpx.AsyncClient | None = None
) -> str:
    """调用 RAG 接口返回 answer 文本；HTTP/解析错误抛异常由上层兜底。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if config.rag_api_token:
        headers["X-API-Key"] = config.rag_api_token
    body = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=RAG_TIMEOUT_SECONDS)
    try:
        response = await client.post(config.rag_api_url, content=body, headers=headers)
        response.raise_for_status()
        return extract_answer(response.text)
    finally:
        if own:
            await client.aclose()


async def handle_message(
    runtime,
    ws_client,
    frame: dict[str, Any],
    *,
    query: Callable[[Any, str], Awaitable[str]] = query_rag,
    gen_req_id: Callable[[str], str] | None = None,
) -> str | None:
    """处理一条智能机器人消息：查 RAG 并流式回复；返回实际回复文本。"""
    text = frame_text(frame)
    if not text:
        return None

    config = runtime.config
    userid = frame_userid(frame)
    row = runtime.db.query_one(
        "SELECT name FROM display_names WHERE kind = 'user' AND value = ?", (userid,)
    )
    user_name = row["name"] if row else userid

    if not config.rag_api_url:
        reply = _failure_text(runtime.db)
    else:
        try:
            answer = await query(config, text)
            reply = (
                compose_reply(config.rag_reply_template, user_name, answer)
                if answer
                else _failure_text(runtime.db)
            )
        except Exception as exc:
            log.warning("智能机器人 RAG 调用失败：%s", exc)
            reply = _failure_text(runtime.db)

    if gen_req_id is None:
        from wecom_aibot_sdk import generate_req_id

        gen_req_id = generate_req_id
    stream_id = gen_req_id("stream")
    await ws_client.reply_stream(frame, stream_id, reply, True)
    return reply


def make_handler(runtime, ws_client, gen_req_id) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def handler(frame: dict[str, Any]) -> None:
        try:
            await handle_message(runtime, ws_client, frame, gen_req_id=gen_req_id)
        except Exception:
            log.exception("智能机器人消息处理异常")

    return handler


async def run_aibot(runtime) -> None:
    """长连接主循环：SDK 内置心跳/重连；runtime.stop 置位后退出。"""
    from wecom_aibot_sdk import WSClient, generate_req_id

    config = runtime.config
    client = WSClient(bot_id=config.aibot_bot_id, secret=config.aibot_secret)
    client.on("message.text", make_handler(runtime, client, generate_req_id))
    client.on("message.mixed", make_handler(runtime, client, generate_req_id))
    client.on("authenticated", lambda: log.info("智能机器人长连接认证成功"))
    client.on("disconnected", lambda reason: log.warning("智能机器人连接断开：%s", reason))
    client.on("error", lambda exc: log.error("智能机器人连接错误：%s", exc))
    await client.connect()
    log.info("智能机器人长连接已建立（bot_id=%s）", config.aibot_bot_id)
    try:
        await runtime.stop.wait()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
