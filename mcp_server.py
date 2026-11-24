# -*- coding: utf-8 -*-
"""企业微信会话内容存档 MCP 服务（FastMCP）。"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.lifespan import lifespan
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from wecom_core import (
    NameResolver,
    WeComConfig,
    all_message_userids,
    count_jsonl_lines,
    download_media,
    export_sessions,
    get_external_group_info,
    get_internal_group_info,
    get_session_messages,
    list_external_groups,
    load_roomids_from_messages,
    load_seq,
    pull_messages,
    query_messages,
    save_seq,
)

# ----------------------------------------------------------------------------
# 常量（可通过环境变量覆盖）
# ----------------------------------------------------------------------------
SERVICE_NAME = "wecom-msg-audit-mcp"
SERVICE_INSTRUCTIONS = (
    "提供企业微信会话内容存档能力：拉取并解密聊天记录、查询本地消息、"
    "获取群聊信息。服务启动时已预配置，直接调用工具即可。"
)

BEARER_TOKEN_ENV = "MCP_BEARER_TOKEN"
ENV_FILE = Path(__file__).resolve().parent / ".env"
HEALTH_ROUTE = "/health"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8331


# ----------------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------------
class BearerTokenVerifier(TokenVerifier):
    """校验 HTTP 模式下的 Bearer Token。"""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._token or token != self._token:
            return None
        return AccessToken(
            token=token,
            client_id="mcp-client",
            scopes=[],
            claims={},
        )


def _read_env_value(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None
    prefix = f"{key}="
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        val = line[len(prefix):].strip().strip('"').strip("'")
        return val or None
    return None


def resolve_bearer_token() -> tuple[str, str]:
    """解析 Bearer Token。

    优先级：进程环境变量 > .env 文件 > 自动生成（仅本次进程有效）。
    返回 (token, source)，其中 source 用于启动日志。
    """
    token = os.getenv(BEARER_TOKEN_ENV)
    if token:
        return token, "env"

    token = _read_env_value(ENV_FILE, BEARER_TOKEN_ENV)
    if token:
        return token, "env-file"

    token = secrets.token_urlsafe(32)
    return token, "generated"


# ----------------------------------------------------------------------------
# 消息格式化
# ----------------------------------------------------------------------------
_SHORT_LABELS: dict[str, str] = {
    "image": "[图片]", "video": "[视频]", "voice": "[语音]",
    "emoticon": "[表情]", "card": "[名片]", "location": "[位置]",
    "redpacket": "[红包]", "meeting": "[会议]", "docmsg": "[在线文档]",
    "calendar": "[日历]", "todo": "[待办]", "vote": "[投票]",
    "collect": "[填表]", "qydisk": "[企业云盘]", "mixed": "[图文混排]",
    "agree": "[同意会话存档]", "disagree": "[不同意会话存档]",
    "revoke": "[撤回消息]",
}


def _human_size(size: int) -> str:
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"


def _extract_content(msg: dict) -> str:
    msgtype = msg.get("msgtype", "")
    if msgtype == "text":
        return msg.get("text", {}).get("content", "")

    if msgtype == "image":
        size = msg.get("image", {}).get("filesize", 0)
        return f"[图片] {_human_size(size)}" if size else "[图片]"

    if msgtype == "video":
        size = msg.get("video", {}).get("filesize", 0)
        return f"[视频] {_human_size(size)}" if size else "[视频]"

    if msgtype == "voice":
        size = msg.get("voice", {}).get("filesize", 0)
        return f"[语音] {_human_size(size)}" if size else "[语音]"

    if msgtype == "emoticon":
        emo = msg.get("emoticon", {})
        size = emo.get("imagesize", 0) or emo.get("filesize", 0)
        desc = (emo.get("md5sum", "") or "")[:8]
        parts = ["[表情]"]
        if size:
            parts.append(_human_size(size))
        if desc:
            parts.append(f"#{desc}")
        return " ".join(parts)

    if msgtype == "file":
        file = msg.get("file", {})
        filename = file.get("filename", "")
        size = file.get("filesize", 0)
        size_str = f" {_human_size(size)}" if size else ""
        return f"[文件] {filename}{size_str}" if filename else f"[文件]{size_str}"

    if msgtype == "link":
        link = msg.get("link", {})
        parts = [p for p in (link.get("title", ""), link.get("description", "")) if p]
        return f"[链接] {' - '.join(parts)}" if parts else "[链接]"

    if msgtype == "sphfeed":
        sph = msg.get("sphfeed", {})
        parts = [p for p in (sph.get("sph_name", ""), sph.get("feed_desc", "")) if p]
        return f"[视频号] {' - '.join(parts)}" if parts else "[视频号]"

    if msgtype == "location":
        addr = msg.get("location", {}).get("address", "")
        return f"[位置] {addr}" if addr else "[位置]"

    if msgtype == "mixed":
        parts: list[str] = []
        for it in msg.get("mixed", {}).get("item", []):
            t = it.get("type", "")
            c = it.get("content", "")
            if t == "text" and c:
                parts.append(c)
            elif t:
                parts.append(_SHORT_LABELS.get(t, f"[{t}]"))
        return f"[混排] {' '.join(parts)}" if parts else "[混排]"

    return _SHORT_LABELS.get(msgtype, f"[{msgtype}]")


def _fmt_msg(
    msg: dict,
    user_names: dict[str, str] | None = None,
    group_names: dict[str, str] | None = None,
) -> str:
    m = msg.get("message", msg)
    if m.get("action") == "switch":
        ts = datetime.fromtimestamp(m.get("time", 0) / 1000).strftime("%m-%d %H:%M:%S")
        return f"[{ts}] {m.get('user', '?')} [切换企业]"

    ts = datetime.fromtimestamp(m.get("msgtime", 0) / 1000).strftime("%m-%d %H:%M:%S")
    sender_id = m.get("from", "?")
    sender = (user_names or {}).get(sender_id, sender_id)
    roomid = m.get("roomid", "")
    content = _extract_content(m)

    if roomid:
        gname = (group_names or {}).get(roomid, roomid)
        return f"[{ts}] {sender} @群 {gname}: {content}"
    return f"[{ts}] {sender}: {content}"


def _format_messages_md(
    messages: list[dict],
    user_names: dict[str, str] | None = None,
    group_names: dict[str, str] | None = None,
) -> str:
    return "\n".join(
        _fmt_msg(m, user_names, group_names)
        for m in messages
        if m.get("message", m).get("action") != "switch"
    )


def _collect_ids(messages: list[dict]) -> tuple[set[str], set[str]]:
    """从消息行集合里收集全部 userid 与 roomid。"""
    user_ids: set[str] = set()
    room_ids: set[str] = set()
    for m in messages:
        msg = m.get("message", m)
        if msg.get("from"):
            user_ids.add(msg["from"])
        if msg.get("roomid"):
            room_ids.add(msg["roomid"])
        for uid in msg.get("tolist") or []:
            if uid:
                user_ids.add(uid)
    return user_ids, room_ids


def _resolve_names(config: WeComConfig, user_ids: set[str], room_ids: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """批量把 userid/roomid 解析为名称；解析不出的保持原 ID。"""
    resolver = NameResolver(config)
    user_names = {uid: name for uid in user_ids if (name := resolver.user_name(uid))}
    group_names = {rid: name for rid in room_ids if (name := resolver.group_name(rid))}
    return user_names, group_names


def _api_error_message(data: dict[str, Any]) -> str:
    errmsg = data.get("errmsg", "未知错误")
    if isinstance(errmsg, str) and errmsg.startswith("ok"):
        return "未知错误"
    return str(errmsg)


def _format_external_group(group_chat: dict[str, Any]) -> str:
    name = group_chat.get("name") or "(无群名)"
    owner = group_chat.get("owner", "?")
    members = group_chat.get("member_list", [])
    lines = [
        f"群名: {name}",
        "类型: 外部客户群",
        f"群主: {owner}",
        f"成员数: {len(members)}",
    ]
    if members:
        lines.append("成员:")
        for m in members[:20]:
            uid = m.get("userid", "?")
            mtype = m.get("type", "")
            lines.append(f"  - {uid}" + (f" ({mtype})" if mtype else ""))
        if len(members) > 20:
            lines.append(f"  ... 共 {len(members)} 人")
    return "\n".join(lines)


def _format_internal_group(data: dict[str, Any]) -> str:
    name = data.get("roomname") or "(无群名)"
    creator = data.get("creator", "?")
    members = data.get("members", [])
    lines = [
        f"群名: {name}",
        "类型: 内部群",
        f"创建者: {creator}",
        f"成员数: {len(members)}",
    ]
    if members:
        lines.append("成员:")
        for m in members[:20]:
            uid = m.get("memberid", m.get("userid", "?"))
            lines.append(f"  - {uid}")
        if len(members) > 20:
            lines.append(f"  ... 共 {len(members)} 人")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# MCP 应用
# ----------------------------------------------------------------------------
@lifespan
async def app_lifespan(server):
    config = WeComConfig.from_env()
    bot_runtime = None
    bot_db = None
    bot_tasks: list = []
    if os.getenv("BOT_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        try:
            from wecom_bot.config import load_bot_config
            from wecom_bot.db import Database
            from wecom_bot.runtime import BotRuntime, seed_defaults

            bot_config = load_bot_config()
            bot_db = Database(bot_config.bot_db_path)
            bot_db.ensure_schema()
            seed_defaults(bot_db, bot_config)
            bot_runtime = BotRuntime(bot_config, bot_db)
            bot_tasks = await bot_runtime.start_loops(config.output_jsonl)
            print(
                f"[{SERVICE_NAME}] 群聊问答机器人已启动（bot 库: {bot_config.bot_db_path}）",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"[{SERVICE_NAME}] 机器人启动失败，问答功能不可用: {exc}", file=sys.stderr)
            bot_runtime = None
            if bot_db:
                bot_db.close()
                bot_db = None
    try:
        yield {"config": config}
    finally:
        if bot_runtime:
            await bot_runtime.shutdown()
        for task in bot_tasks:
            task.cancel()
        if bot_db:
            bot_db.close()


_bearer_token, _bearer_source = resolve_bearer_token()

mcp = FastMCP(
    SERVICE_NAME,
    instructions=SERVICE_INSTRUCTIONS,
    lifespan=app_lifespan,
    auth=BearerTokenVerifier(_bearer_token),
)


# ----------------------------------------------------------------------------
# 工具定义
# ----------------------------------------------------------------------------
@mcp.tool
def wecom_get_config_status(ctx: Context) -> dict[str, Any]:
    """检查服务是否就绪（消息拉取、群聊查询等能力是否可用）。"""
    config = ctx.lifespan_context["config"]
    return config.client_status()


@mcp.tool
def wecom_pull_messages(
    ctx: Context,
    limit: int = 100,
    save_to_file: bool = True,
    preview: int = 20,
) -> dict[str, Any]:
    """从企业微信会话存档 SDK 拉取并解密聊天记录。

    从上次保存的 seq 位置继续拉取，解密后返回消息列表，并自动更新 seq。

    Args:
        limit: 单次拉取条数，最大 1000
        save_to_file: 是否将解密消息追加保存到本地
        preview: 返回内容中预览的消息条数（全部消息都已落盘，可用 wecom_query_messages 分页查看）
    """
    config = ctx.lifespan_context["config"]
    result = pull_messages(config, limit=limit, save_to_file=save_to_file)

    lines = [
        f"拉取完成: seq {result.start_seq}→{result.end_seq}, "
        f"成功{result.success_count}条, 失败{result.fail_count}条, "
        f"去重跳过{result.duplicate_count}条",
    ]
    if result.messages:
        shown = result.messages[: max(preview, 0)]
        if shown:
            lines.append("")
            lines.append(f"预览 {len(shown)}/{len(result.messages)} 条:")
            lines.append(_format_messages_md(shown))
        if len(result.messages) > len(shown):
            lines.append("")
            lines.append(
                f"其余 {len(result.messages) - len(shown)} 条已保存到本地，"
                "用 wecom_query_messages(keyword=..., limit=..., offset=...) 分页查看，"
                "或 wecom_list_sessions 按会话归类浏览。"
            )
    if result.errors:
        lines.append("")
        lines.append("错误:")
        for e in result.errors:
            lines.append(f"  - seq={e.get('seq')}: {e.get('error')}")
    return {"result": "\n".join(lines)}


@mcp.tool
def wecom_get_seq(ctx: Context) -> str:
    """获取当前消息拉取进度 seq。"""
    config = ctx.lifespan_context["config"]
    current = load_seq(config.seq_file)
    return (
        f"当前消息拉取进度 seq: {current}\n\n"
        "用法说明:\n"
        "- 调用 wecom_pull_messages 从当前 seq 继续拉取新消息\n"
        "- 调用 wecom_set_seq 可重置或跳过历史消息的起始位置\n"
        "- seq 会在每次成功拉取后自动更新为最新值"
    )


@mcp.tool
def wecom_set_seq(ctx: Context, seq: int) -> str:
    """设置消息拉取起始 seq（用于重置或跳过历史消息）。

    Args:
        seq: 新的 seq 值
    """
    config = ctx.lifespan_context["config"]
    save_seq(config.seq_file, seq)
    return (
        f"已将 seq 设置为 {seq}\n\n"
        "用法说明:\n"
        f"- 下次调用 wecom_pull_messages 将从 seq {seq} 开始拉取\n"
        "- 如需查看最新进度，调用 wecom_get_seq"
    )


@mcp.tool
def wecom_query_messages(
    ctx: Context,
    roomid: str | None = None,
    from_user: str | None = None,
    msgtype: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查询本地已保存的解密聊天记录。

    Args:
        roomid: 按群聊 roomid 过滤
        from_user: 按发送者 userid 过滤
        msgtype: 按消息类型过滤（text、image、file 等）
        keyword: 按关键词全文搜索
        limit: 返回条数上限
        offset: 分页偏移
    """
    config = ctx.lifespan_context["config"]
    data = query_messages(
        config.output_jsonl,
        roomid=roomid,
        from_user=from_user,
        msgtype=msgtype,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    lines = [
        f"查询结果: 共{data['total']}条 (offset={data['offset']}, limit={data['limit']})"
    ]
    if data["messages"]:
        lines.append("")
        lines.append(_format_messages_md(data["messages"]))
    return {"result": "\n".join(lines)}


@mcp.tool
def wecom_extract_roomids(ctx: Context) -> dict[str, Any]:
    """从本地消息记录中提取所有群聊 roomid。"""
    config = ctx.lifespan_context["config"]
    roomids = sorted(load_roomids_from_messages(config.output_jsonl))
    return {
        "count": len(roomids),
        "roomids": roomids,
        "result": (
            "\n".join([f"共发现 {len(roomids)} 个群聊 roomid:", *(f"  - {r}" for r in roomids)])
            if roomids
            else "当前消息记录中暂无群聊。"
        ),
    }


def _fmt_ts_range(first: int | None, last: int | None) -> str:
    if not first or not last:
        return "-"
    fmt = lambda t: datetime.fromtimestamp(t / 1000).strftime("%y-%m-%d %H:%M")
    return f"{fmt(first)} ~ {fmt(last)}"


@mcp.tool
def wecom_list_sessions(ctx: Context, write_files: bool = True) -> dict[str, Any]:
    """把本地消息按会话拆分归类（群聊按 roomid、单聊按收发双方），返回会话清单。

    Args:
        write_files: 是否同时把各会话导出为 data/sessions/<key>.jsonl 分文件
    """
    config = ctx.lifespan_context["config"]
    sessions_dir = str(Path(config.output_jsonl).parent / "sessions")
    data = export_sessions(
        config.output_jsonl,
        sessions_dir,
        write_files=write_files,
    )

    # 名称解析：群名 + 单聊双方姓名（无凭据/缓存未命中时降级显示 ID）
    resolver = NameResolver(config)
    lines: list[str] = []
    if not data["sessions"]:
        lines.append("本地暂无消息记录，先调用 wecom_pull_messages 拉取。")
    else:
        lines.append(f"共 {data['total']} 条消息，归为 {len(data['sessions'])} 个会话:")
        lines.append("")
        for s in data["sessions"]:
            key = s["session_key"]
            label = key.split(":", 1)[1] if ":" in key else key
            if key.startswith("group:"):
                kind = "群聊"
                label = resolver.group_name(label) or label
            elif key.startswith("dm:"):
                kind = "单聊"
                pair = label.split("+")
                label = " + ".join(resolver.user_name(u) or u for u in pair)
            else:
                kind = "其他"
            files_part = f" -> {s['file']}" if s.get("file") else ""
            lines.append(
                f"  [{kind}] {label}: {s['count']}条 "
                f"({_fmt_ts_range(s['first_msgtime'], s['last_msgtime'])}){files_part}"
            )
        lines.append("")
        lines.append(
            "会话键用 wecom_get_session(session_key=...) 查询；"
            "若显示的是 ID，可用 wecom_refresh_names 拉取通讯录/群名后重试。"
        )

    return {"total": data["total"], "sessions": data["sessions"], "result": "\n".join(lines)}


@mcp.tool
def wecom_get_session(
    ctx: Context,
    session_key: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查看单个会话的消息（按时间正序），session_key 来自 wecom_list_sessions。

    Args:
        session_key: 会话键，如 group:wrOgQh... 或 dm:wangwu+zhangsan
        limit: 返回条数上限
        offset: 分页偏移
    """
    config = ctx.lifespan_context["config"]
    data = get_session_messages(
        config.output_jsonl,
        session_key,
        limit=limit,
        offset=offset,
    )
    user_ids, room_ids = _collect_ids(data["messages"])
    user_names, group_names = _resolve_names(config, user_ids, room_ids)
    lines = [
        f"会话 {session_key}: 共{data['total']}条 (offset={data['offset']}, limit={data['limit']})"
    ]
    if data["messages"]:
        lines.append("")
        lines.append(_format_messages_md(data["messages"], user_names, group_names))
    elif data["total"] == 0:
        lines.append("会话不存在或无消息，先用 wecom_list_sessions 查看可用会话。")
    return {"total": data["total"], "messages": data["messages"], "result": "\n".join(lines)}


@mcp.tool
def wecom_refresh_names(ctx: Context) -> dict[str, Any]:
    """拉取通讯录成员姓名与本地消息涉及的群名，写入本地缓存（数据/name_cache.json）。

    通讯录姓名需要 CONTACT_SECRET（通讯录同步 Secret）；群名用会话存档 Secret
    或客户联系 Secret。刷新后 wecom_list_sessions / wecom_get_session 会显示名称。
    """
    config = ctx.lifespan_context["config"]
    resolver = NameResolver(config)
    lines: list[str] = []

    users_error = ""
    users_count = -1
    try:
        users_count = resolver.refresh_users()
        lines.append(f"通讯录成员姓名: 已缓存 {users_count} 人")
    except RuntimeError as e:
        users_error = str(e)
        lines.append(f"通讯录成员姓名: 刷新失败 - {e}")

    roomids = sorted(load_roomids_from_messages(config.output_jsonl))
    if roomids:
        ok, failed = resolver.refresh_groups(roomids)
        lines.append(f"群名称: 成功 {ok}/{len(roomids)} 个")
        if failed:
            lines.append(f"  未解析到群名: {', '.join(failed[:10])}"
                         + (f" ... 共{len(failed)}个" if len(failed) > 10 else ""))
    else:
        lines.append("群名称: 本地暂无消息记录，先拉取消息后再刷新。")

    st = resolver.stats()
    lines.append(f"缓存现状: {st['users']} 人 / {st['groups']} 个群 -> {config.names_cache_file}")
    if users_count < 0:
        lines.append("")
        lines.append(
            "提示: 在 .env 配置 CONTACT_SECRET（管理后台 -> 安全与管理 -> 管理工具 -> 通讯录同步）"
            "后重建容器即可解析成员姓名。"
        )
    return {
        "users_cached": st["users"],
        "groups_cached": st["groups"],
        "users_refresh_error": users_error,
        "result": "\n".join(lines),
    }


@mcp.tool
def wecom_get_group_info(
    ctx: Context,
    roomid: str,
    group_type: str = "auto",
) -> dict[str, Any]:
    """获取群聊详情（外部客户群或内部群）。

    Args:
        roomid: 群聊 roomid
        group_type: 群类型，可选 auto（自动尝试）、external（外部客户群）、internal（内部群）
    """
    config = ctx.lifespan_context["config"]
    result: dict[str, Any] = {"roomid": roomid, "found": False}

    if group_type in ("auto", "external"):
        try:
            ext = get_external_group_info(config, roomid)
            if ext.get("errcode") == 0:
                group_chat = ext.get("group_chat", {})
                result.update(
                    found=True,
                    group_type="external",
                    name=group_chat.get("name") or "",
                    member_count=len(group_chat.get("member_list", [])),
                    result=_format_external_group(group_chat),
                )
                return result
            result.setdefault("errors", []).append(
                f"外部客户群: {_api_error_message(ext)}"
            )
        except RuntimeError as e:
            result.setdefault("errors", []).append(str(e))

    if group_type in ("auto", "internal"):
        try:
            internal = get_internal_group_info(config, roomid)
            if internal.get("errcode") == 0:
                result.update(
                    found=True,
                    group_type="internal",
                    name=internal.get("roomname") or "",
                    member_count=len(internal.get("members", [])),
                    result=_format_internal_group(internal),
                )
                return result
            result.setdefault("errors", []).append(
                f"内部群: {_api_error_message(internal)}"
            )
        except RuntimeError as e:
            result.setdefault("errors", []).append(str(e))

    errors = result.get("errors", [])
    if errors:
        result["result"] = f"未找到群聊 {roomid}\n" + "\n".join(f"- {e}" for e in errors)
    else:
        result["result"] = f"未找到群聊 {roomid}"
    return result


@mcp.tool
def wecom_list_external_groups(
    ctx: Context,
    limit: int = 100,
) -> dict[str, Any]:
    """列出外部客户群。

    Args:
        limit: 返回群数量上限
    """
    config = ctx.lifespan_context["config"]
    groups = list_external_groups(config, limit=limit)
    lines = [f"外部客户群共 {len(groups)} 个:"]
    for g in groups:
        chat_id = g.get("chat_id", "?")
        status = g.get("status", "")
        lines.append(f"  - {chat_id}" + (f" (status={status})" if status != "" else ""))
    return {
        "count": len(groups),
        "groups": [{"chat_id": g.get("chat_id"), "status": g.get("status")} for g in groups],
        "result": "\n".join(lines),
    }


@mcp.tool
def wecom_download_media(
    ctx: Context,
    sdkfileid: str,
    save_to: str | None = None,
) -> str:
    """下载消息中的媒体文件（图片、视频、语音、文件等）。

    通过 sdkfileid 下载媒体内容。图片上传到 R2 并返回预签名访问链接，其他文件保存到磁盘。
    也可指定 save_to 路径将文件保存到指定位置。

    Args:
        sdkfileid: 媒体文件 ID（从消息的 sdkfileids 字段获取）
        save_to: 保存路径，不传则图片返回 R2 预签名链接、其他文件自动保存
    """
    config = ctx.lifespan_context["config"]
    return download_media(config, sdkfileid=sdkfileid, save_to=save_to)


@mcp.custom_route(HEALTH_ROUTE, methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": SERVICE_NAME})


_MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MEDIA_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".bin": "application/octet-stream",
}


@mcp.custom_route("/media", methods=["GET"])
async def media_list(request):
    """列出本地已保存的媒体文件。"""
    config = WeComConfig.from_env()
    media_dir = Path(config.output_jsonl).parent / "media"
    files = (
        sorted(p.name for p in media_dir.iterdir() if p.is_file())
        if media_dir.exists()
        else []
    )
    return JSONResponse({"count": len(files), "files": files})


@mcp.custom_route("/media/{name}", methods=["GET"])
async def media_file(request):
    """按文件名访问本地保存的媒体文件（图片降级存储）。"""
    name = request.path_params["name"]
    if not _MEDIA_NAME_RE.fullmatch(name) or ".." in name:
        return JSONResponse({"error": "非法文件名"}, status_code=400)

    config = WeComConfig.from_env()
    media_dir = Path(config.output_jsonl).parent / "media"
    path = media_dir / name
    if not path.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)

    mime = _MEDIA_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)


# ----------------------------------------------------------------------------
# 企微回调验证（自建应用「接收消息服务器URL」保存时的 echostr 验证）
# 配置可信 IP 的前置门槛；验证通过后本路由无需保持公网可达
# ----------------------------------------------------------------------------
def _wecom_echo_verify(
    token: str,
    aes_key_b64: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str | None:
    """校验签名并解密 echostr，返回明文；失败返回 None。"""
    import base64
    import hashlib

    from Crypto.Cipher import AES

    sign = hashlib.sha1("".join(sorted([token, timestamp, nonce, echostr])).encode()).hexdigest()
    if sign != msg_signature:
        return None

    key = base64.b64decode(aes_key_b64 + "=")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    plain = cipher.decrypt(base64.b64decode(echostr))
    # 明文结构: 16字节随机 + 4字节网络序长度 + 消息体 + receiveid
    msg_len = int.from_bytes(plain[16:20], "big")
    return plain[20 : 20 + msg_len].decode("utf-8")


@mcp.custom_route("/wecom_callback", methods=["GET"])
async def wecom_callback(request):
    token = os.getenv("WECOM_CALLBACK_TOKEN", "")
    aes_key = os.getenv("WECOM_CALLBACK_AES_KEY", "")
    if not token or not aes_key:
        return JSONResponse({"error": "回调未配置：.env 缺少 WECOM_CALLBACK_TOKEN / WECOM_CALLBACK_AES_KEY"}, status_code=400)

    q = request.query_params
    plain = _wecom_echo_verify(
        token,
        aes_key,
        q.get("msg_signature", ""),
        q.get("timestamp", ""),
        q.get("nonce", ""),
        q.get("echostr", ""),
    )
    if plain is None:
        return PlainTextResponse("signature verify failed", status_code=403)
    # 必须原样返回解密后的明文（纯文本 body）
    return PlainTextResponse(plain)


# ----------------------------------------------------------------------------
# Web 操作台（本地仪表盘，无鉴权，仅限本机访问）
# ----------------------------------------------------------------------------
def _dashboard_config() -> WeComConfig:
    return WeComConfig.from_env()


def _session_label(resolver: NameResolver, key: str) -> tuple[str, str]:
    """返回 (类型, 显示名)。"""
    label = key.split(":", 1)[1] if ":" in key else key
    if key.startswith("group:"):
        return "群聊", resolver.group_name(label) or label
    if key.startswith("dm:"):
        return "单聊", " + ".join(resolver.user_name(u) or u for u in label.split("+"))
    return "其他", label


@mcp.custom_route("/", methods=["GET"])
async def dashboard(request):
    return HTMLResponse(_DASHBOARD_HTML)


@mcp.custom_route("/api/status", methods=["GET"])
async def api_status(request):
    config = _dashboard_config()
    missing = config.validate_for_pull()
    corp = config.corp_id
    masked = f"{corp[:4]}****{corp[-4:]}" if len(corp) > 8 else corp
    resolver = NameResolver(config)
    unnamed = sorted(all_message_userids(config.output_jsonl) - resolver.all_users().keys()) if count_jsonl_lines(config.output_jsonl) else []
    return JSONResponse(
        {
            "ready": not missing,
            "missing": missing,
            "corp_id": masked,
            "seq": load_seq(config.seq_file),
            "messages": count_jsonl_lines(config.output_jsonl),
            "unnamed": unnamed,
            "contact_secret": bool(config.contact_secret),
            "external_secret": bool(config.external_contact_secret),
        }
    )


@mcp.custom_route("/api/sessions", methods=["GET"])
async def api_sessions(request):
    config = _dashboard_config()
    data = export_sessions(config.output_jsonl, "", write_files=False)
    resolver = NameResolver(config)
    sessions = []
    for s in data["sessions"]:
        kind, label = _session_label(resolver, s["session_key"])
        sessions.append(
            {
                "session_key": s["session_key"],
                "kind": kind,
                "label": label,
                "count": s["count"],
                "first_msgtime": s["first_msgtime"],
                "last_msgtime": s["last_msgtime"],
            }
        )
    return JSONResponse({"total": data["total"], "sessions": sessions})


@mcp.custom_route("/api/session_messages", methods=["GET"])
async def api_session_messages(request):
    key = request.query_params.get("key", "")
    offset = max(int(request.query_params.get("offset", "0") or 0), 0)
    limit = min(max(int(request.query_params.get("limit", "50") or 50), 1), 200)
    config = _dashboard_config()
    data = get_session_messages(config.output_jsonl, key, limit=limit, offset=offset)
    user_ids, room_ids = _collect_ids(data["messages"])
    user_names, group_names = _resolve_names(config, user_ids, room_ids)
    lines = (
        _format_messages_md(data["messages"], user_names, group_names).split("\n")
        if data["messages"]
        else []
    )
    return JSONResponse(
        {"total": data["total"], "offset": offset, "limit": limit, "lines": lines}
    )


@mcp.custom_route("/api/pull", methods=["POST"])
async def api_pull(request):
    config = _dashboard_config()
    try:
        result = pull_messages(config, limit=100, save_to_file=True)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    lines = [
        f"seq {result.start_seq}→{result.end_seq} | "
        f"成功 {result.success_count} / 失败 {result.fail_count} / 去重跳过 {result.duplicate_count}"
    ]
    shown = result.messages[:20]
    if shown:
        user_ids, room_ids = _collect_ids(shown)
        user_names, group_names = _resolve_names(config, user_ids, room_ids)
        lines.append("")
        lines.append(f"预览 {len(shown)}/{len(result.messages)} 条:")
        lines.extend(_format_messages_md(shown, user_names, group_names).split("\n"))
    if result.errors:
        lines.append("")
        for e in result.errors:
            lines.append(f"错误 seq={e.get('seq')}: {e.get('error')}")

    unnamed = []
    resolver = NameResolver(config)
    unnamed = sorted(all_message_userids(config.output_jsonl) - resolver.all_users().keys())
    if unnamed:
        lines.append("")
        lines.append(f"⚠ 当前数据涉及 {len(unnamed)} 个未命名 userid，请在下方「名称映射管理」中补填:")
        lines.extend(f"  - {uid}" for uid in unnamed[:20])
    return JSONResponse({"lines": lines, "unnamed": unnamed})


@mcp.custom_route("/api/refresh_names", methods=["POST"])
async def api_refresh_names(request):
    config = _dashboard_config()
    resolver = NameResolver(config)
    lines: list[str] = []
    try:
        n = resolver.refresh_users()
        lines.append(f"通讯录成员姓名: 已缓存 {n} 人")
    except RuntimeError as e:
        lines.append(f"通讯录成员姓名: 刷新失败 - {e}")

    roomids = sorted(load_roomids_from_messages(config.output_jsonl))
    if roomids:
        ok, failed = resolver.refresh_groups(roomids)
        lines.append(f"群名称: 成功 {ok}/{len(roomids)} 个")
        if failed:
            lines.append(f"  未解析到群名: {', '.join(failed[:10])}")
    else:
        lines.append("群名称: 本地暂无消息记录")
    st = resolver.stats()
    lines.append(f"缓存现状: {st['users']} 人 / {st['groups']} 个群")
    return JSONResponse({"lines": lines})


@mcp.custom_route("/api/names", methods=["GET"])
async def api_names(request):
    """名称映射清单 + 消息中出现但未命名的 userid。"""
    config = _dashboard_config()
    resolver = NameResolver(config)
    users = resolver.all_users()
    missing = sorted(all_message_userids(config.output_jsonl) - users.keys())
    return JSONResponse({"users": users, "missing": missing})


@mcp.custom_route("/api/names", methods=["POST"])
async def api_set_name(request):
    """设置/更新/删除 userid 的姓名。body: {"userid": "...", "name": "..."}，name 为空则删除。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)

    userid = str(body.get("userid") or "").strip()
    name = str(body.get("name") or "").strip()
    if not userid:
        return JSONResponse({"error": "userid 不能为空"}, status_code=400)

    config = _dashboard_config()
    resolver = NameResolver(config)
    resolver.set_user(userid, name)
    users = resolver.all_users()
    missing = sorted(all_message_userids(config.output_jsonl) - users.keys())
    action = "已删除" if not name else f"已设置为 {name}"
    return JSONResponse({"ok": True, "userid": userid, "action": action, "users": users, "missing": missing})


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>企微会话存档 · 操作台</title>
<style>
:root { --bg:#f5f6f8; --card:#fff; --line:#e3e6ea; --ink:#1f2329; --sub:#8a919f; --blue:#3370ff; --green:#34c724; --red:#f54a45; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--ink); }
.wrap { max-width:1100px; margin:24px auto; padding:0 16px; }
h1 { font-size:18px; margin-bottom:12px; }
.bar { display:flex; gap:8px; margin-bottom:12px; }
button { border:0; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:14px; color:#fff; background:var(--blue); }
button.gray { background:#6b7280; } button:disabled { opacity:.5; cursor:not-allowed; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:12px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { padding:3px 10px; border-radius:12px; background:#eef1f5; color:var(--sub); font-size:12px; }
.chip.ok { background:#e8f8e4; color:#1d7a12; } .chip.bad { background:#fdeceb; color:#b42318; }
.cols { display:flex; gap:12px; align-items:flex-start; }
.left { width:340px; flex-shrink:0; } .right { flex:1; }
.sitem { padding:8px 10px; border:1px solid var(--line); border-radius:6px; margin-bottom:8px; cursor:pointer; }
.sitem:hover, .sitem.active { border-color:var(--blue); background:#f0f5ff; }
.sitem .t { font-size:12px; color:var(--sub); }
.empty { color:var(--sub); text-align:center; padding:30px 0; }
#msgs div { padding:6px 8px; border-bottom:1px solid var(--line); word-break:break-all; white-space:pre-wrap; }
.pager { display:flex; gap:8px; align-items:center; margin-top:10px; }
.pager button { background:#eef1f5; color:var(--ink); padding:5px 12px; }
#log { font-family:Consolas,monospace; font-size:12px; white-space:pre-wrap; color:#444; max-height:180px; overflow:auto; }
.pagerinfo { color:var(--sub); font-size:12px; }
.nrow { display:flex; gap:8px; align-items:center; padding:5px 0; border-bottom:1px solid var(--line); }
.nuid { flex:2; font-family:Consolas,monospace; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--sub); }
.nname { flex:1; padding:5px 8px; border:1px solid var(--line); border-radius:5px; font-size:13px; }
button.mini { padding:4px 10px; font-size:12px; } button.mini.red { background:var(--red); }
.mtip { background:#fff8e6; border:1px solid #f0dfad; border-radius:6px; padding:8px 10px; font-size:12px; color:#8a6d1a; margin-bottom:8px; word-break:break-all; }
.muid { display:inline-block; font-family:Consolas,monospace; background:#fff; border:1px solid var(--line); border-radius:4px; padding:1px 6px; margin:3px 4px 0 0; }
#alert { background:#fdeceb; border:1px solid #f5b7b4; color:#b42318; border-radius:8px; padding:10px 14px; margin-bottom:12px; font-size:13px; cursor:pointer; word-break:break-all; }
#alert b { color:#b42318; }
</style>
</head>
<body>
<div class="wrap">
  <div id="alert" style="display:none"></div>
  <h1>企微会话存档 · 操作台</h1>
  <div class="card">
    <div class="chips" id="status">加载中...</div>
  </div>
  <div class="bar">
    <button id="btn-pull" onclick="pull()">拉取消息</button>
    <button id="btn-names" class="gray" onclick="refreshNames()">刷新名称缓存</button>
    <button class="gray" onclick="loadAll()">刷新列表</button>
  </div>
  <div class="card"><div id="log">就绪。</div></div>
  <div class="cols">
    <div class="left card"><b>会话列表</b><div id="sessions" style="margin-top:10px"></div></div>
    <div class="right card">
      <b id="cur-title">消息</b>
      <div id="msgs" style="margin-top:10px"></div>
      <div class="pager">
        <button id="prev" onclick="page(-1)">上一页</button>
        <button id="next" onclick="page(1)">下一页</button>
        <span class="pagerinfo" id="pagerinfo"></span>
      </div>
    </div>
  </div>
  <div class="card">
    <b>名称映射管理</b>
    <div class="pagerinfo" style="margin:4px 0 8px">手工姓名永久生效（不受 24h 缓存过期影响）；姓名留空保存即删除映射。</div>
    <div id="missing-tip"></div>
    <div id="names-table"></div>
    <div class="bar" style="margin:8px 0 0">
      <input id="new-uid" placeholder="userid" style="flex:2;padding:7px 10px;border:1px solid var(--line);border-radius:6px;font-family:Consolas,monospace;font-size:12px" onkeydown="if(event.key==='Enter')addName()">
      <input id="new-name" placeholder="姓名" style="flex:1;padding:7px 10px;border:1px solid var(--line);border-radius:6px" onkeydown="if(event.key==='Enter')addName()">
      <button onclick="addName()">添加</button>
    </div>
  </div>
</div>
<script>
const PAGE = 50;
let curKey = null, curOffset = 0, curTotal = 0;
const $ = id => document.getElementById(id);
const esc = s => { const d = document.createElement("div"); d.textContent = s; return d.innerHTML.replace(/'/g, "&#39;").replace(/"/g, "&quot;"); };
const fmtTime = t => t ? new Date(t).toLocaleString("zh-CN", {hour12:false}) : "-";

function log(lines) { $("log").textContent = lines.join("\\n"); $("log").scrollTop = 0; }

function showAlert(ids) {
  const el = $("alert");
  if (!ids || !ids.length) { el.style.display = "none"; return; }
  el.innerHTML = `<b>⚠ 有 ${ids.length} 个 userid 还没有设置姓名</b>（消息里会显示为 ID）：<br>` +
    ids.map(u => `<span class="muid">${esc(u)}</span>`).join("") +
    `<br>点击此处前往「名称映射管理」补填 →`;
  el.style.display = "block";
  el.onclick = () => document.querySelectorAll(".card")[4]?.scrollIntoView({behavior: "smooth"});
}

function hideAlert() { $("alert").style.display = "none"; }

async function loadStatus() {
  const s = await (await fetch("/api/status")).json();
  const chips = [];
  chips.push(`<span class="chip ${s.ready ? "ok" : "bad"}">${s.ready ? "拉取就绪" : "未就绪: " + s.missing.join("、")}</span>`);
  chips.push(`<span class="chip">企业 ${esc(s.corp_id || "-")}</span>`);
  chips.push(`<span class="chip">seq ${s.seq}</span>`);
  chips.push(`<span class="chip">本地消息 ${s.messages} 条</span>`);
  if (s.unnamed && s.unnamed.length) {
    chips.push(`<span class="chip bad">未命名ID ${s.unnamed.length} 个</span>`);
    showAlert(s.unnamed);
  } else {
    hideAlert();
  }
  chips.push(`<span class="chip ${s.contact_secret ? "ok" : ""}">${s.contact_secret ? "通讯录已配" : "通讯录未配"}</span>`);
  chips.push(`<span class="chip ${s.external_secret ? "ok" : ""}">${s.external_secret ? "客户联系已配" : "客户联系未配"}</span>`);
  $("status").innerHTML = chips.join("");
}

async function loadSessions() {
  const d = await (await fetch("/api/sessions")).json();
  const el = $("sessions");
  if (!d.sessions.length) {
    el.innerHTML = `<div class="empty">暂无会话<br><br>点上方「拉取消息」试试</div>`;
    return;
  }
  el.innerHTML = d.sessions.map(s =>
    `<div class="sitem" onclick="openSession('${esc(s.session_key)}', '${esc(s.label)}')">
       <div>${s.kind === "群聊" ? "👥" : s.kind === "单聊" ? "💬" : "❔"} <b>${esc(s.label)}</b> <span class="t">(${s.count}条)</span></div>
       <div class="t">${fmtTime(s.first_msgtime)} ~ ${fmtTime(s.last_msgtime)}</div>
     </div>`).join("");
}

async function loadMessages() {
  if (!curKey) return;
  const d = await (await fetch(`/api/session_messages?key=${encodeURIComponent(curKey)}&offset=${curOffset}&limit=${PAGE}`)).json();
  curTotal = d.total;
  $("msgs").innerHTML = d.lines.length ? d.lines.map(l => `<div>${esc(l)}</div>`).join("")
                                       : `<div class="empty">该会话无消息</div>`;
  $("pagerinfo").textContent = d.total ? `${curOffset + 1}-${Math.min(curOffset + PAGE, d.total)} / 共 ${d.total} 条` : "";
  $("prev").disabled = curOffset <= 0;
  $("next").disabled = curOffset + PAGE >= d.total;
}

function openSession(key, label) {
  curKey = key; curOffset = 0;
  $("cur-title").textContent = "消息 · " + label;
  document.querySelectorAll(".sitem").forEach(el => el.classList.remove("active"));
  event.currentTarget.classList.add("active");
  loadMessages();
}

function page(delta) { curOffset = Math.max(0, curOffset + delta * PAGE); loadMessages(); }

async function pull() {
  const btn = $("btn-pull"); btn.disabled = true;
  $("log").textContent = "拉取中...";
  try {
    const r = await fetch("/api/pull", { method: "POST" });
    const d = await r.json();
    log(d.error ? ["拉取失败: " + d.error] : d.lines);
    if (d.unnamed && d.unnamed.length) showAlert(d.unnamed); else hideAlert();
    await Promise.all([loadStatus(), loadSessions(), loadNames()]);
  } finally { btn.disabled = false; }
}

async function refreshNames() {
  const btn = $("btn-names"); btn.disabled = true;
  try {
    const d = await (await fetch("/api/refresh_names", { method: "POST" })).json();
    log(d.lines);
    await loadSessions();
  } finally { btn.disabled = false; }
}

function loadAll() { loadStatus(); loadSessions(); loadNames(); }

async function loadNames() {
  const d = await (await fetch("/api/names")).json();
  const rows = Object.entries(d.users);
  $("names-table").innerHTML = rows.length
    ? rows.map(([uid, name]) =>
        `<div class="nrow" data-uid="${esc(uid)}">
           <span class="nuid" title="${esc(uid)}">${esc(uid)}</span>
           <input class="nname" value="${esc(name)}" onkeydown="if(event.key==='Enter')saveName('${esc(uid)}',this)">
           <button class="mini" onclick="saveName('${esc(uid)}', this.parentNode.querySelector('.nname'))">保存</button>
           <button class="mini red" onclick="delName('${esc(uid)}')">删除</button>
         </div>`).join("")
    : `<div class="pagerinfo">暂无映射</div>`;
  $("missing-tip").innerHTML = d.missing.length
    ? `<div class="mtip">⚠ 消息中出现但未命名的 ID（${d.missing.length} 个）：<br>` +
      d.missing.map(uid => `<span class="muid">${esc(uid)}</span>`).join("") + `</div>`
    : "";
}

async function saveName(uid, input) {
  const name = input.value.trim();
  await fetch("/api/names", { method: "POST", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify({userid: uid, name}) });
  await loadNames(); await loadSessions();
  if (curKey) loadMessages();
}

async function delName(uid) {
  await fetch("/api/names", { method: "POST", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify({userid: uid, name: ""}) });
  await loadNames(); await loadSessions();
}

async function addName() {
  const uid = $("new-uid").value.trim(), name = $("new-name").value.trim();
  if (!uid) return;
  await fetch("/api/names", { method: "POST", headers: {"Content-Type": "application/json"},
                              body: JSON.stringify({userid: uid, name}) });
  $("new-uid").value = ""; $("new-name").value = "";
  await loadNames(); await loadSessions();
  if (curKey) loadMessages();
}

loadAll();
</script>
</body>
</html>
"""


def run_server() -> None:
    """MCP 服务入口（供 console_scripts 调用）。"""
    transport = os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT).lower()
    host = os.getenv("MCP_HOST", DEFAULT_HOST)
    port = int(os.getenv("MCP_PORT", str(DEFAULT_PORT)))

    if transport == "http":
        if _bearer_source == "generated":
            print(
                f"[{SERVICE_NAME}] 未检测到 {BEARER_TOKEN_ENV}，已自动生成 Bearer Token "
                f"（仅本次进程有效）:\n  {_bearer_token}\n"
                f"[{SERVICE_NAME}] 建议通过 {BEARER_TOKEN_ENV} 环境变量显式注入。",
                file=sys.stderr,
            )
        else:
            print(
                f"[{SERVICE_NAME}] HTTP 模式启动于 http://{host}:{port}/mcp "
                f"（鉴权来源: {_bearer_source}）",
                file=sys.stderr,
            )
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    run_server()
