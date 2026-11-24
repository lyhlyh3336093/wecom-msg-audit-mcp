"""Bot 配置（自 gateway/config.py 裁剪适配）。

采集相关配置（corpid/secret/私钥）由本项目主配置负责，此处只保留
机器人自身运行所需的项；全部可选，均有缺省值。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _merge(environ: Mapping[str, str], env_file: Path) -> dict[str, str]:
    merged = _read_env_file(env_file)
    for key, value in environ.items():
        if value != "":
            merged[key] = value
    return merged


def _split(raw: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


@dataclass(frozen=True)
class BotConfig:
    bot_db_path: Path
    bot_self_userids: tuple[str, ...] = field(default_factory=tuple)
    mention_keywords: tuple[str, ...] = ("@智能小助手",)
    merge_window_seconds: int = 30
    flush_interval_seconds: float = 5.0
    ingest_interval_seconds: float = 2.0
    webhook_rate_per_minute: int = 20
    rag_api_url: str = ""
    rag_api_token: str = ""
    rag_reply_template: str = "@${user_name} ${answer}"
    aibot_bot_id: str = ""
    aibot_secret: str = ""


def load_bot_config(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> BotConfig:
    environ = os.environ if environ is None else environ
    env_file = DEFAULT_ENV_FILE if env_file is None else env_file
    values = _merge(environ, env_file)

    return BotConfig(
        bot_db_path=Path(values.get("BOT_DB_PATH") or "data/gateway.db").expanduser(),
        bot_self_userids=_split(values.get("BOT_SELF_USERIDS")),
        mention_keywords=_split(values.get("BOT_MENTION_KEYWORDS"))
        or ("@智能小助手",),
        merge_window_seconds=int(values.get("MERGE_WINDOW_SECONDS") or 30),
        flush_interval_seconds=float(values.get("FLUSH_INTERVAL_SECONDS") or 5.0),
        ingest_interval_seconds=float(values.get("BOT_INGEST_INTERVAL_SECONDS") or 2.0),
        webhook_rate_per_minute=int(values.get("BOT_WEBHOOK_RATE_PER_MINUTE") or 20),
        rag_api_url=values.get("RAG_API_URL", ""),
        rag_api_token=values.get("RAG_API_TOKEN", ""),
        rag_reply_template=values.get("RAG_REPLY_TEMPLATE") or "@${user_name} ${answer}",
        aibot_bot_id=values.get("BOT_AIBOT_ID", ""),
        aibot_secret=values.get("BOT_AIBOT_SECRET", ""),
    )
