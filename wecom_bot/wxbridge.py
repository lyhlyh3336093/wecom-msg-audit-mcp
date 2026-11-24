"""微信群 wxauto 渠道适配器（U1）— 独立进程运行。

用法:  uv run python -m wecom_bot.wxbridge

架构（计划 KTD1-KTD5）:
- 独立于企微链路的进程；直接复用 wecom_bot.aibot 的 query_rag/compose_reply 自调 RAG
- wxauto 监听 callback 内不可发送（官方约束）: callback 只做关键词匹配、去重、入队；
  独立工作线程串行消费 → 调 RAG → 限速发送
- 消息去重: (群名, 发送者, 内容, 时间) 有界键集合
- 积压保护: 队列深度上限（满则丢最旧）；连续 N 次 RAG 失败进入熔断窗口，窗口内直接回复兜底文案
- 失败文案读 WXBOT_FAILURE_TEXT（默认内置），不读容器写入的 gateway.db（消除宿主/容器 SQLite 并发）
- wxautox4 为付费闭源 API: 单测以 fake 客户端注入；真机集成点集中在 _connect_wxauto
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .aibot import compose_reply, query_rag
from .config import DEFAULT_ENV_FILE, _merge, _split

log = logging.getLogger(__name__)

DEFAULT_FAILURE_TEXT = "抱歉，这条消息暂时没能处理，已经记录，稍后跟进。"
DEFAULT_MENTION_KEYWORD = "@智能小助手"

# wxbridge 允许读取的环境变量白名单（最小化凭据面——宿主机进程不接触采集类密钥）
_ALLOWED_ENV_KEYS = (
    "WXBOT_LISTEN_CHATS", "WXBOT_MENTION_KEYWORDS", "WXBOT_FAILURE_TEXT",
    "WXBOT_RATE_SECONDS", "WXBOT_QUEUE_MAX", "WXBOT_CIRCUIT_THRESHOLD",
    "WXBOT_CIRCUIT_COOLDOWN_SECONDS", "WXBOT_ENV_FILE",
    "RAG_API_URL", "RAG_API_TOKEN", "RAG_REPLY_TEMPLATE",
)


@dataclass(frozen=True)
class WXBotConfig:
    rag_api_url: str = ""
    rag_api_token: str = ""
    rag_reply_template: str = "@${user_name} ${answer}"
    listen_chats: tuple[str, ...] = ()
    keywords: tuple[str, ...] = (DEFAULT_MENTION_KEYWORD,)
    failure_text: str = DEFAULT_FAILURE_TEXT
    rate_seconds: float = 2.0
    queue_max: int = 10
    circuit_threshold: int = 3
    circuit_cooldown_seconds: float = 60.0


def load_wxbot_config(
    environ: dict[str, str] | None = None,
    env_file: Path | None = None,
) -> WXBotConfig:
    """读取 WXBOT_*/RAG_* 白名单键；配置对象不携带任何其他环境变量。"""
    environ = dict(environ) if environ is not None else None
    if environ is None:
        import os

        environ = {k: v for k, v in os.environ.items() if k in _ALLOWED_ENV_KEYS}
    env_file = env_file if env_file is not None else DEFAULT_ENV_FILE
    merged = _merge(environ, env_file)
    merged = {k: v for k, v in merged.items() if k in _ALLOWED_ENV_KEYS}
    return WXBotConfig(
        rag_api_url=merged.get("RAG_API_URL", ""),
        rag_api_token=merged.get("RAG_API_TOKEN", ""),
        rag_reply_template=merged.get("RAG_REPLY_TEMPLATE") or "@${user_name} ${answer}",
        listen_chats=_split(merged.get("WXBOT_LISTEN_CHATS")),
        keywords=_split(merged.get("WXBOT_MENTION_KEYWORDS")) or (DEFAULT_MENTION_KEYWORD,),
        failure_text=merged.get("WXBOT_FAILURE_TEXT") or DEFAULT_FAILURE_TEXT,
        rate_seconds=float(merged.get("WXBOT_RATE_SECONDS") or 2.0),
        queue_max=int(merged.get("WXBOT_QUEUE_MAX") or 10),
        circuit_threshold=int(merged.get("WXBOT_CIRCUIT_THRESHOLD") or 3),
        circuit_cooldown_seconds=float(merged.get("WXBOT_CIRCUIT_COOLDOWN_SECONDS") or 60.0),
    )


@dataclass
class MessageTask:
    chat: str
    sender: str
    content: str


class Deduper:
    """有界消息键集合；check 返回 True 表示首次出现。"""

    def __init__(self, max_keys: int = 512) -> None:
        self._max = max_keys
        self._seen: OrderedDict[tuple, None] = OrderedDict()

    def check_and_add(self, key: tuple) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True


class CircuitBreaker:
    """连续失败熔断：达到阈值后进入冷却窗口，窗口内 is_open 返回 True。"""

    def __init__(self, threshold: int, cooldown_seconds: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._consecutive = 0
        self._opened_at: float | None = None

    def record_success(self, now: float) -> None:
        self._consecutive = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        self._consecutive += 1
        if self._consecutive >= self._threshold and self._opened_at is None:
            self._opened_at = now

    def is_open(self, now: float) -> bool:
        if self._opened_at is None:
            return False
        if now - self._opened_at >= self._cooldown:
            # 冷却期结束，放行试探；失败计数保留，再失败即再次熔断
            self._opened_at = None
            return False
        return True


class WxBridge:
    """核心适配器。wx 客户端与 RAG 函数均可注入（单测用 fake）。"""

    def __init__(
        self,
        config: WXBotConfig,
        *,
        wx: Any | None = None,
        rag_fn: Callable[[WXBotConfig, str], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._wx = wx
        self._rag_fn = rag_fn or self._rag_via_asyncio
        self._clock = clock
        self._sleeper = sleeper
        self._tasks: queue.Queue[MessageTask] = queue.Queue(maxsize=config.queue_max)
        self._dedupe = Deduper()
        self._breaker = CircuitBreaker(config.circuit_threshold, config.circuit_cooldown_seconds)
        self._last_send = 0.0
        self._stop = threading.Event()

    # ---- RAG（async 函数的同步包装，worker 线程内调用） ----
    @staticmethod
    def _rag_via_asyncio(config: WXBotConfig, question: str) -> str:
        return asyncio.run(query_rag(config, question))

    # ---- 入队侧（callback 线程调用，绝不发送） ----
    def _matches(self, content: str) -> bool:
        stripped = content.strip()
        return any(k.strip().lower() in stripped.lower() for k in self.config.keywords if k.strip())

    def enqueue_message(self, chat: str, sender: str, content: str, ts: str = "") -> bool:
        """callback 只调这个：匹配 → 去重 → 入队（满则丢最旧）。返回是否入队。"""
        content = (content or "").strip()
        if not content or not self._matches(content):
            return False
        if not self._dedupe.check_and_add((chat, sender, content, ts)):
            return False
        task = MessageTask(chat=chat, sender=sender, content=content)
        try:
            self._tasks.put_nowait(task)
        except queue.Full:
            try:
                dropped = self._tasks.get_nowait()
                log.warning("队列已满，丢弃最旧任务（群=%s）", dropped.chat)
            except queue.Empty:
                pass
            self._tasks.put_nowait(task)
        return True

    # ---- 消费侧（worker 线程） ----
    def _rate_wait(self) -> None:
        gap = self._clock() - self._last_send
        remain = self.config.rate_seconds - gap
        if remain > 0:
            self._sleeper(remain)
        self._last_send = self._clock()

    def send_text(self, chat: str, text: str) -> None:
        if self._wx is None:
            raise RuntimeError("wx 客户端未注入")
        self._wx.SendMsg(text, who=chat)

    def _send_failure(self, chat: str) -> None:
        try:
            self._rate_wait()
            self.send_text(chat, self.config.failure_text)
        except Exception:
            log.exception("发送兜底文案失败（群=%s）", chat)

    def _process_task(self, task: MessageTask) -> None:
        now = self._clock()
        if self._breaker.is_open(now):
            log.warning("熔断窗口内，直接回复兜底文案（群=%s）", task.chat)
            self._send_failure(task.chat)
            return
        try:
            answer = self._rag_fn(self.config, task.content)
        except Exception as exc:
            self._breaker.record_failure(self._clock())
            log.warning("RAG 调用失败（群=%s）：%s", task.chat, exc)
            self._send_failure(task.chat)
            return
        if not answer:
            self._breaker.record_failure(self._clock())
            self._send_failure(task.chat)
            return
        self._breaker.record_success(self._clock())
        reply = compose_reply(self.config.rag_reply_template, task.sender, answer)
        try:
            self._rate_wait()
            self.send_text(task.chat, reply)
        except Exception:
            log.exception("发送回复失败（群=%s）", task.chat)

    def worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process_task(task)

    def start_worker(self) -> threading.Thread:
        t = threading.Thread(target=self.worker_loop, name="wxbridge-worker", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    # ---- 真机集成（唯一触碰 wxautox4 的位置；API 形状以真机验收为准） ----
    def make_callback(self) -> Callable[[Any], None]:
        def callback(msg: Any) -> None:
            try:
                chat = str(getattr(msg, "chat_name", "") or "")
                sender = str(getattr(msg, "sender", "") or "")
                content = str(getattr(msg, "content", "") or "")
                ts = str(getattr(msg, "time", "") or "")
                self.enqueue_message(chat=chat, sender=sender, content=content, ts=ts)
            except Exception:
                log.exception("处理监听消息异常")
        return callback

    def connect(self) -> None:
        """连接 PC 微信并注册目标群监听。wxautox4 的 import 名/类名以真机为准。"""
        if self._wx is not None:
            return
        errors: list[str] = []
        wx = None
        for mod_name in ("wxautox4", "wxauto4", "wxauto"):
            try:
                module = __import__(mod_name)
                wx = module.WeChat()
                log.info("已连接 PC 微信（包 %s）", mod_name)
                break
            except Exception as exc:
                errors.append(f"{mod_name}: {exc}")
        if wx is None:
            raise RuntimeError("无法初始化 wxauto 客户端，依次尝试: " + "; ".join(errors))
        self._wx = wx
        for chat_name in self.config.listen_chats:
            wx.AddListenChat(chat_name, callback=self.make_callback())
            log.info("已注册群监听: %s", chat_name)

    def run_forever(self) -> None:
        if not self.config.listen_chats:
            raise RuntimeError("WXBOT_LISTEN_CHATS 未配置目标群")
        if not self.config.rag_api_url:
            raise RuntimeError("RAG_API_URL 未配置")
        self.connect()
        self.start_worker()
        log.info("wxbridge 运行中（群=%s, 限速=%ss）", self.config.listen_chats, self.config.rate_seconds)
        while not self._stop.is_set():
            self._sleeper(1.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_wxbot_config()
    WxBridge(config).run_forever()


if __name__ == "__main__":
    main()
