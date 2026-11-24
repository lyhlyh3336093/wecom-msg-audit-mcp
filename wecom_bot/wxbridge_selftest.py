"""wxbridge 单测（U1/V2）: python -m wecom_bot.wxbridge_selftest

覆盖计划 U1 测试场景: 关键词匹配/去重/空文本/队列满丢最旧（edge）、
RAG 异常与空答案兜底、熔断窗口、发送异常不杀循环（error）、
限速间隔（integration, 时钟注入）、配置最小化凭据面。
"""

from __future__ import annotations

import sys

from wecom_bot.wxbridge import (
    DEFAULT_FAILURE_TEXT,
    CircuitBreaker,
    MessageTask,
    WxBridge,
    WXBotConfig,
    load_wxbot_config,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"  {detail}"))
    if cond:
        PASS += 1
    else:
        FAIL += 1


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeWx:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_times = fail_times

    def SendMsg(self, text: str, who: str = "") -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("simulated send failure")
        self.sent.append((who, text))


def make_bridge(
    rag_fn=None,
    wx=None,
    *,
    queue_max: int = 10,
    rate_seconds: float = 2.0,
    circuit_threshold: int = 3,
    clock: FakeClock | None = None,
) -> tuple[WxBridge, FakeClock]:
    clock = clock or FakeClock()
    config = WXBotConfig(
        rag_api_url="http://rag/query",
        rag_api_token="k",
        listen_chats=("测试群",),
        queue_max=queue_max,
        rate_seconds=rate_seconds,
        circuit_threshold=circuit_threshold,
    )
    default_rag = lambda cfg, q: "合规答案"  # noqa: E731
    bridge = WxBridge(config, wx=wx or FakeWx(), rag_fn=rag_fn or default_rag, clock=clock)
    return bridge, clock


def drain(bridge: WxBridge) -> None:
    while not bridge._tasks.empty():
        bridge._process_task(bridge._tasks.get_nowait())


def test_enqueue_and_reply() -> None:
    print("=== 入队与回复（happy path）===")
    bridge, _ = make_bridge()
    ok = bridge.enqueue_message("测试群", "张三", "@智能小助手 合规政策有哪些", ts="1")
    check("关键词命中入队", ok is True)
    drain(bridge)
    wx = bridge._wx
    check("回复渲染 @昵称+答案", wx.sent == [("测试群", "@张三 合规答案")], repr(wx.sent))

    check("无关键词不入队", bridge.enqueue_message("测试群", "张三", "闲聊一句") is False)
    check("空文本不入队", bridge.enqueue_message("测试群", "张三", "   ") is False)
    bridge.enqueue_message("测试群", "张三", "@智能小助手 再问", ts="2")
    check("同键去重不入队", bridge.enqueue_message("测试群", "张三", "@智能小助手 再问", ts="2") is False)


def test_queue_overflow() -> None:
    print("=== 队列满丢最旧 ===")
    bridge, _ = make_bridge(queue_max=2)
    bridge.enqueue_message("群", "u", "@智能小助手 一", ts="1")
    bridge.enqueue_message("群", "u", "@智能小助手 二", ts="2")
    bridge.enqueue_message("群", "u", "@智能小助手 三", ts="3")
    contents = [t.content for t in list(bridge._tasks.queue)]
    check("最旧任务被丢弃", contents == ["@智能小助手 二", "@智能小助手 三"], str(contents))


def test_failure_paths() -> None:
    print("=== 失败路径 ===")
    calls = {"n": 0}

    def failing_rag(cfg, q):
        calls["n"] += 1
        raise ConnectionError("rag down")

    bridge, clock = make_bridge(rag_fn=failing_rag)
    for ts in ("1", "2", "3", "4"):
        bridge.enqueue_message("群", "u", f"@智能小助手 问题{ts}", ts=ts)
    drain(bridge)
    wx = bridge._wx
    check("连续失败均回兜底文案", all(t == DEFAULT_FAILURE_TEXT for _, t in wx.sent), str(wx.sent))
    check("兜底文案共 4 条", len(wx.sent) == 4, str(len(wx.sent)))
    check("熔断已触发（不再调 RAG）", calls["n"] == bridge.config.circuit_threshold and calls["n"] == 3, str(calls["n"]))
    check("第 4 条走熔断窗口", "问题4" not in str(calls["n"]))

    # 熔断冷却后放行试探
    clock.advance(61.0)
    check("冷却期结束放行", bridge._breaker.is_open(clock()) is False)

    # 空答案走兜底
    bridge2, _ = make_bridge(rag_fn=lambda cfg, q: "")
    bridge2.enqueue_message("群", "u", "@智能小助手 问", ts="1")
    drain(bridge2)
    check("空答案回兜底文案", bridge2._wx.sent == [("群", DEFAULT_FAILURE_TEXT)], str(bridge2._wx.sent))


def test_send_failure_recovery() -> None:
    print("=== 发送异常不杀循环 ===")
    wx = FakeWx(fail_times=1)
    bridge, _ = make_bridge(wx=wx)
    bridge.enqueue_message("群", "u", "@智能小助手 一", ts="1")
    bridge.enqueue_message("群", "u", "@智能小助手 二", ts="2")
    drain(bridge)
    check("第一条发送失败被吞", len(wx.sent) == 1, str(wx.sent))
    check("第二条正常发送", wx.sent[0] == ("群", "@u 合规答案"), str(wx.sent))


def test_rate_limit() -> None:
    print("=== 限速间隔（时钟注入）===")
    sleeps: list[float] = []
    clock = FakeClock()
    bridge, _ = make_bridge(rate_seconds=2.0, clock=clock)
    bridge._sleeper = lambda s: sleeps.append(s) or clock.advance(s)
    bridge.enqueue_message("群", "u", "@智能小助手 问", ts="1")
    bridge._process_task(bridge._tasks.get_nowait())
    bridge.enqueue_message("群", "u", "@智能小助手 再问", ts="2")
    bridge._process_task(bridge._tasks.get_nowait())
    check("第二次发送前触发限速等待", len(sleeps) >= 1 and abs(sleeps[0] - 2.0) < 0.01, str(sleeps))


def test_circuit_breaker_unit() -> None:
    print("=== 熔断器单元 ===")
    cb = CircuitBreaker(threshold=2, cooldown_seconds=30)
    clock = FakeClock()
    check("初始不熔断", cb.is_open(clock()) is False)
    cb.record_failure(clock())
    check("未达阈值不熔断", cb.is_open(clock()) is False)
    cb.record_failure(clock())
    check("达阈值熔断", cb.is_open(clock()) is True)
    clock.advance(29.0)
    check("冷却期内仍熔断", cb.is_open(clock()) is True)
    clock.advance(2.0)
    check("冷却期满放行", cb.is_open(clock()) is False)
    cb.record_success(clock())
    cb.record_failure(clock())
    check("成功后计数清零", cb.is_open(clock()) is False)


def test_config_minimal_surface() -> None:
    print("=== 配置最小化凭据面 ===")
    config = load_wxbot_config(
        environ={
            "RAG_API_URL": "http://127.0.0.1:8001/api/query",
            "RAG_API_TOKEN": "k",
            "WXBOT_RATE_SECONDS": "3",
            "WXBOT_FAILURE_TEXT": "自定义兜底",
            # 混入采集类密钥，验证白名单过滤
            "MSGAUDIT_SECRET": "should-not-appear",
            "CONTACT_SECRET": "should-not-appear",
        },
        env_file=None,
    )
    check("白名单键生效", config.rag_api_url == "http://127.0.0.1:8001/api/query" and config.rate_seconds == 3.0)
    check("自定义兜底文案生效", config.failure_text == "自定义兜底")
    fields = {f for f in dir(config) if not f.startswith("_") and not callable(getattr(config, f))}
    leaked = [f for f in fields if "secret" in f.lower() or "audit" in f.lower() or "contact" in f.lower()]
    check("配置对象无采集类字段", leaked == [] and "MSGAUDIT_SECRET" not in repr(config), str(fields))


def main() -> None:
    test_enqueue_and_reply()
    test_queue_overflow()
    test_failure_paths()
    test_send_failure_recovery()
    test_rate_limit()
    test_circuit_breaker_unit()
    test_config_minimal_surface()
    print(f"\n=== 结果: PASS={PASS} FAIL={FAIL} ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
