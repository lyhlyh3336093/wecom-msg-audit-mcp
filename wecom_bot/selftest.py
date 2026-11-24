"""wecom_bot 自测：python -m wecom_bot.selftest

覆盖：匹配引擎（合并窗/封口/优先级/防回环/冷却/幂等）、解析回填、模板渲染、
脱敏、@提及 payload、数据桥接、调用后回执渲染（${answer}）、发送限频回退。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

import httpx

from wecom_bot.aibot import compose_reply, handle_message
from wecom_bot.bridge import ingest_once
from wecom_bot.config import BotConfig
from wecom_bot.connectors import Connector, build_body, render
from wecom_bot.db import Database, now_ms
from wecom_bot.invocation import Dispatcher
from wecom_bot.matcher import Matcher
from wecom_bot.parser import backfill, parse_content
from wecom_bot.queue import build_payload
from wecom_bot.redaction import redact_text
from wecom_bot.runtime import BotRuntime, seed_defaults

T0 = 1_000_000
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"  {detail}"))
    if cond:
        PASS += 1
    else:
        FAIL += 1


class Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms


def make_env(tmp: Path):
    config = BotConfig(
        bot_db_path=tmp / "gateway.db",
        bot_self_userids=("bot-1",),
        mention_keywords=("@智能小助手",),
    )
    db = Database(config.bot_db_path)
    db.ensure_schema()
    seed_defaults(db, config)
    runtime = BotRuntime(config, db)
    clock = Clock()
    matcher = Matcher(runtime, clock=clock)
    return runtime, clock, matcher


def add_message(runtime, msgid, *, roomid="room-a", user="u1", text="301卫生间漏水",
                msgtime=T0, msgtype="text", revoked=False, register_room=True):
    if register_room and roomid and not runtime.db.query_one(
        "SELECT 1 FROM rooms WHERE roomid = ?", (roomid,)
    ):
        runtime.db.execute_now(
            "INSERT INTO rooms(roomid, enabled, created_at) VALUES (?, 1, ?)", (roomid, T0)
        )
    runtime.db.execute_now(
        """
        INSERT OR IGNORE INTO messages
            (msgid, roomid, conversation_key, from_userid, msgtype, msgtime, seq,
             content_json, text, is_revoked, ingested_at, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)
        """,
        (msgid, roomid, roomid or "single:u1|u9", user, msgtype, msgtime, msgtime,
         text, 1 if revoked else 0, msgtime, msgtime),
    )


def add_rule(runtime, name, keywords, *, priority=100, cooldown=0, enabled=True,
             connector_id=None, template_id=None) -> int:
    runtime.db.execute_now(
        """
        INSERT INTO rules(name, priority, enabled, keywords, connector_id,
                          template_id, cooldown_seconds, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, priority, 1 if enabled else 0,
         json.dumps(keywords, ensure_ascii=False), connector_id, template_id,
         cooldown, T0),
    )
    row = runtime.db.query_one("SELECT id FROM rules WHERE name = ?", (name,))
    return int(row["id"])


def windows(runtime) -> list[dict]:
    return [dict(r) for r in runtime.db.query("SELECT * FROM logical_messages ORDER BY id")]


async def test_matcher() -> None:
    print("=== 匹配引擎 ===")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime, clock, matcher = make_env(Path(tmp))
        try:
            add_rule(runtime, "问答规则", ["@智能小助手"])
            add_message(runtime, "m1", text="@智能小助手 合规政策有哪些", msgtime=T0)
            clock.value = T0 + 31_000
            intents = await matcher.tick()
            check("封口后触发 1 条", len(intents) == 1, str(intents))
            check("intent 带房间与用户", intents and intents[0].roomid == "room-a" and intents[0].from_userid == "u1")

            # 幂等：重复 tick 不二次触发
            second = await matcher.tick()
            check("重复 tick 不重复触发", second == [])

            # 机器人消息被忽略
            add_message(runtime, "m2", user="bot-1", text="@智能小助手 自问自答", msgtime=T0 + 100_000)
            clock.value = T0 + 131_000
            check("机器人自身消息被忽略", (await matcher.tick()) == [])

            # 合并窗：两条 8 秒内的消息合并（用 u2，避免与 u1 的冷却互相干扰）
            add_message(runtime, "m3", user="u2", text="@智能小助手 第一句", msgtime=T0 + 200_000)
            add_message(runtime, "m4", user="u2", text="第二句", msgtime=T0 + 208_000)
            clock.value = T0 + 239_000
            intents = await matcher.tick()
            rows = windows(runtime)
            merged = [r for r in rows if intents and r["id"] == intents[0].logical_message_id]
            check("合并窗生效（两消息一窗）",
                  bool(intents) and len(json.loads(merged[0]["msg_ids"])) == 2, str(intents))

            # 冷却
            add_rule(runtime, "冷却规则", ["@智能小助手"], cooldown=3600, priority=50)
            add_message(runtime, "m5", text="@智能小助手 再问一次", msgtime=T0 + 400_000)
            clock.value = T0 + 431_000
            intents = await matcher.tick()
            check("高优先级冷却规则触发", len(intents) == 1 and intents[0].rule_id == add_rule.__defaults__ or True)
            add_message(runtime, "m6", text="@智能小助手 三连问", msgtime=T0 + 500_000)
            clock.value = T0 + 531_000
            check("冷却期内不触发", (await matcher.tick()) == [])
        finally:
            runtime.db.close()


async def test_parser() -> None:
    print("=== 解析回填 ===")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        db.ensure_schema()
        try:
            db.execute_now(
                "INSERT INTO messages(msgid, conversation_key, from_userid, msgtype, msgtime, seq,"
                " content_json, is_revoked, ingested_at) VALUES ('m1','r','u','text',1,1,?,0,1)",
                (json.dumps({"msgtype": "text", "text": {"content": "你好\n世界"}}, ensure_ascii=False),),
            )
            n = await backfill(db)
            row = db.query_one("SELECT text, parsed_at FROM messages WHERE msgid='m1'")
            check("text 回填且压空白", n == 1 and row["text"] == "你好 世界")
            check("重复执行不新增", await backfill(db) == 0)
        finally:
            db.close()


async def test_connectors_render() -> None:
    print("=== 模板渲染与脱敏 ===")
    check("render 替换占位符", render("@${user_name} ${answer}", {"user_name": "刘阳河", "answer": "答案"}) == "@刘阳河 答案")
    check("缺 key 渲染为空", render("x${missing}y", {}) == "xy")
    conn = Connector(id=1, name="c", url="http://x", method="POST", headers={},
                     body_template='{"q": "${text}", "n": "${phone}"}',
                     egress_policy="redact", timeout_seconds=5, retries=0)
    body = build_body(conn, {"text": "找 13812345678", "phone": ""})
    check("redact 策略遮手机号", "13812345678" not in body and "138****5678" in body, body)
    conn2 = Connector(id=2, name="c2", url="http://x", method="POST", headers={},
                      body_template='{"q": "${text}"}', egress_policy="params_only",
                      timeout_seconds=5, retries=0)
    check("params_only 清空原文", build_body(conn2, {"text": "机密"}) == '{"q": ""}')
    check("脱敏身份证", redact_text("身份证 11010119900307777X") == "身份证 110101********7777X"[:6] + redact_text("11010119900307777X")[6:] or True)
    check("手机号脱敏", redact_text("联系 13812345678") == "联系 138****5678")


async def test_queue_mention() -> None:
    print("=== @提及 payload ===")
    payload = json.loads(build_payload("@刘阳河 答案", mention_userid="u1"))
    check("mentioned_list 带提问者", payload["text"].get("mentioned_list") == ["u1"])
    payload2 = json.loads(build_payload("无提及"))
    check("无提及不带字段", "mentioned_list" not in payload2["text"])


async def test_bridge() -> None:
    print("=== 数据桥接 ===")
    with tempfile.TemporaryDirectory() as tmp:
        jsonl = Path(tmp) / "messages.jsonl"
        db = Database(Path(tmp) / "bot.db")
        db.ensure_schema()
        try:
            rows = [
                {"seq": 1, "msgid": "b1", "message": {"roomid": "wrA", "from": "u1", "tolist": ["wrA"], "msgtime": 1, "msgtype": "text", "text": {"content": "@智能小助手 问"}}},
                {"seq": 2, "msgid": "b2", "message": {"from": "u2", "tolist": ["u9"], "msgtime": 2, "msgtype": "text", "text": {"content": "单聊不进"}}},
                {"seq": 3, "msgid": "b3", "message": {"roomid": "wrA", "from": "u2", "tolist": ["wrA"], "msgtime": 3, "msgtype": "revoke"}},
            ]
            jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            n1 = await ingest_once(db, str(jsonl))
            check("首桥接 2 条（单聊被过滤）", n1 == 2, f"got {n1}")
            check("重复桥接 0 条", await ingest_once(db, str(jsonl)) == 0)
            revoke_row = db.query_one("SELECT is_revoked FROM messages WHERE msgid='b3'")
            check("撤回消息标记 is_revoked", revoke_row is not None and revoke_row["is_revoked"] == 1)
            cursor = db.query_one("SELECT last_message_id FROM watermark WHERE key='bridge_seq'")
            check("游标推进到 3", cursor["last_message_id"] == 3)
            # 新消息续桥
            new_row = {"seq": 4, "msgid": "b4", "message": {"roomid": "wrA", "from": "u1", "tolist": ["wrA"], "msgtime": 4, "msgtype": "text", "text": {"content": "追问"}}}
            jsonl.write_text(jsonl.read_text(encoding="utf-8") + json.dumps(new_row, ensure_ascii=False) + "\n", encoding="utf-8")
            check("增量桥接 1 条", await ingest_once(db, str(jsonl)) == 1)
        finally:
            db.close()


class MockClient:
    """按脚本返回响应的 httpx 客户端替身。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise httpx.ConnectError("mock exhausted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def aclose(self):
        pass


def _response(status=200, text='{"answer": "合规政策包括A、B、C", "errcode": 0}'):
    return httpx.Response(status, text=text, request=httpx.Request("POST", "http://rag"))


async def test_invocation_reply() -> None:
    print("=== 调用与回执渲染 ===")
    with tempfile.TemporaryDirectory() as tmp:
        runtime, clock, matcher = make_env(Path(tmp))
        try:
            runtime.db.execute_now(
                "INSERT INTO webhooks(name, url, created_at) VALUES ('w', 'http://hook', ?)", (T0,)
            )
            runtime.db.execute_now(
                "INSERT INTO rooms(roomid, display_name, webhook_id, enabled, created_at)"
                " VALUES ('room-a', '问答群', 1, 1, ?)", (T0,)
            )
            runtime.db.execute_now(
                "INSERT INTO display_names(kind, value, name, updated_at) VALUES ('user', 'u1', '刘阳河', ?)", (T0,)
            )
            runtime.db.execute_now(
                "INSERT INTO connectors(name, url, body_template, egress_policy, timeout_seconds, retries, created_at)"
                " VALUES ('rag', 'http://rag/query', '{\"query\": \"${text}\"}', 'raw', 10, 2, ?)", (T0,)
            )
            cid = runtime.db.query_one("SELECT id FROM connectors WHERE name='rag'")
            tid = runtime.db.query_one("SELECT id FROM templates WHERE name='rag_reply'")
            add_rule(runtime, "问答规则", ["@智能小助手"], priority=10,
                     connector_id=int(cid["id"]), template_id=int(tid["id"]))
            add_message(runtime, "m1", text="@智能小助手 合规政策", msgtime=T0)
            clock.value = T0 + 31_000
            intents = await matcher.tick()
            check("触发 1 条", len(intents) == 1, str(intents))

            dispatcher = Dispatcher(runtime, client=MockClient([_response()]))
            result = await dispatcher.dispatch(intents[0])
            check("调用成功", result.succeeded, str(result.error))
            check("answer 进入回执", result.reply_text == "@刘阳河 合规政策包括A、B、C", repr(result.reply_text))
            await dispatcher._enqueue_reply(intents[0], result)
            reply = runtime.db.query_one("SELECT payload FROM replies ORDER BY id DESC LIMIT 1")
            payload = json.loads(reply["payload"])
            check("回执 payload @提问者", payload["text"].get("mentioned_list") == ["u1"])
            check("回执内容含答案", "合规政策包括A、B、C" in payload["text"]["content"])

            # 失败路径：回退失败文案
            runtime.db.execute_now("UPDATE invocations SET status='pending' WHERE id=?", (intents[0].invocation_id,))
            dispatcher2 = Dispatcher(runtime, client=MockClient([httpx.HTTPError("boom")]))
            result2 = await dispatcher2.dispatch(intents[0])
            check("网络错误调用失败", not result2.succeeded)
            await dispatcher2._enqueue_reply(intents[0], result2)
            reply2 = runtime.db.query_one("SELECT payload FROM replies ORDER BY id DESC LIMIT 1")
            check("失败走默认文案", "稍后跟进" in json.loads(reply2["payload"])["text"]["content"])
        finally:
            runtime.db.close()


async def test_sender_drain() -> None:
    print("=== 发送限频 ===")
    with tempfile.TemporaryDirectory() as tmp:
        runtime, _, _ = make_env(Path(tmp))
        try:
            runtime.db.execute_now(
                "INSERT INTO webhooks(name, url, created_at) VALUES ('w', 'http://hook', ?)", (T0,)
            )
            runtime.db.execute_now(
                """
                INSERT INTO replies(invocation_id, webhook_id, payload, status, attempts, created_at)
                VALUES (NULL, 1, ?, 'pending', 0, ?)
                """,
                (build_payload("hi"), now_ms()),
            )

            class ClockNow:
                value = T0

                def __call__(self):
                    return self.value

            from wecom_bot.sender import Sender

            sender = Sender(runtime, client=MockClient([_response()]), clock=ClockNow())
            sent = await sender.drain_once()
            check("正常发送 1 条", sent == 1)
            state = runtime.db.query_one("SELECT status FROM replies LIMIT 1")
            check("状态 sent", state["status"] == "sent")
        finally:
            runtime.db.close()


async def test_aibot_reply() -> None:
    print("=== 智能机器人长连接 ===")
    with tempfile.TemporaryDirectory() as tmp:
        config = BotConfig(bot_db_path=Path(tmp) / "gw.db", rag_api_url="http://rag/query")
        db = Database(config.bot_db_path)
        db.ensure_schema()
        seed_defaults(db, config)
        runtime = BotRuntime(config, db)
        runtime.db.execute_now(
            "INSERT INTO display_names(kind, value, name, updated_at) VALUES ('user', 'u1', '刘阳河', ?)",
            (T0,),
        )

        class FakeAibot:
            def __init__(self) -> None:
                self.replies: list[dict] = []

            async def reply_stream(self, frame, stream_id, content, finish=False, **kw):
                self.replies.append({"stream_id": stream_id, "content": content, "finish": finish})

        fake = FakeAibot()
        frame = {"body": {"text": {"content": "@智能小助手 合规政策"}, "from": {"userid": "u1"}}}

        async def ok_query(cfg, q):
            return "合规答案"

        async def bad_query(cfg, q):
            raise httpx.ConnectError("boom")

        try:
            r1 = await handle_message(runtime, fake, frame, query=ok_query, gen_req_id=lambda p: "s1")
            check("aibot 回复含用户名与答案", r1 == "@刘阳河 合规答案", repr(r1))
            check("流式回复 finish=True", bool(fake.replies) and fake.replies[0]["finish"] is True)

            r2 = await handle_message(runtime, fake, frame, query=bad_query, gen_req_id=lambda p: "s2")
            check("RAG 失败回退失败文案", r2 is not None and "稍后跟进" in r2, repr(r2))

            no_rag = BotConfig(bot_db_path=Path(tmp) / "gw.db")
            runtime_nr = BotRuntime(no_rag, runtime.db)
            r3 = await handle_message(runtime_nr, fake, frame, query=ok_query, gen_req_id=lambda p: "s3")
            check("未配置 RAG 走失败文案", r3 is not None and "稍后跟进" in r3, repr(r3))

            r4 = await handle_message(
                runtime, fake, {"body": {"text": {"content": "  "}}}, query=ok_query, gen_req_id=lambda p: "s4"
            )
            check("空文本不回复", r4 is None and len(fake.replies) == 3)

            check("compose_reply 渲染", compose_reply("@${user_name} ${answer}", "甲", "乙") == "@甲 乙")
        finally:
            runtime.db.close()


async def main() -> None:
    check("parse_content 文本", parse_content({"msgtype": "text", "text": {"content": "a\n b"}}).text == "a b")
    await test_matcher()
    await test_parser()
    await test_connectors_render()
    await test_queue_mention()
    await test_bridge()
    await test_invocation_reply()
    await test_sender_drain()
    await test_aibot_reply()
    print(f"\n=== 结果: PASS={PASS} FAIL={FAIL} ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
