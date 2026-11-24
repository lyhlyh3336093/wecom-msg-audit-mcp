"""数据库表结构（自 gateway/models.py 移植，删去 collector_state / whitelist）。

差异说明：
- messages 表由 bridge.py 从本项目拉取数据写入，非 gateway 自带采集器；
- 群范围控制走 rooms.enabled（gateway 用 whitelist 在采集侧过滤）。
"""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msgid TEXT NOT NULL UNIQUE,
    roomid TEXT,
    conversation_key TEXT NOT NULL,
    from_userid TEXT NOT NULL,
    tolist TEXT,
    msgtype TEXT NOT NULL,
    msgtime INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    text TEXT,
    is_revoked INTEGER NOT NULL DEFAULT 0,
    ingested_at INTEGER NOT NULL,
    parsed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(roomid, msgtime);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(from_userid, msgtime);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_key, msgtime);

CREATE TABLE IF NOT EXISTS logical_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roomid TEXT NOT NULL,
    from_userid TEXT NOT NULL,
    merged_text TEXT NOT NULL,
    msg_ids TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0,
    matched_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logical_window
    ON logical_messages(roomid, from_userid, window_end);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    last_sent_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roomid TEXT NOT NULL UNIQUE,
    display_name TEXT,
    webhook_id INTEGER REFERENCES webhooks(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'POST',
    headers TEXT,
    body_template TEXT NOT NULL,
    egress_policy TEXT NOT NULL DEFAULT 'redact'
        CHECK (egress_policy IN ('raw', 'redact', 'params_only')),
    timeout_seconds REAL NOT NULL DEFAULT 10,
    retries INTEGER NOT NULL DEFAULT 2,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'group',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    keywords TEXT NOT NULL,
    connector_id INTEGER REFERENCES connectors(id),
    template_id INTEGER REFERENCES templates(id),
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_msg_id INTEGER NOT NULL REFERENCES logical_messages(id),
    rule_id INTEGER NOT NULL REFERENCES rules(id),
    connector_id INTEGER REFERENCES connectors(id),
    request_redacted TEXT,
    response TEXT,
    errcode INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'succeeded', 'failed', 'canceled', 'skipped')),
    latency_ms INTEGER,
    error TEXT,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    UNIQUE (logical_msg_id, rule_id)
);

CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_id INTEGER REFERENCES invocations(id),
    webhook_id INTEGER REFERENCES webhooks(id),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed', 'canceled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    errcode INTEGER,
    error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);

CREATE TABLE IF NOT EXISTS cooldowns (
    userid TEXT NOT NULL,
    rule_id INTEGER NOT NULL,
    last_fired_at INTEGER NOT NULL,
    PRIMARY KEY (userid, rule_id)
);

CREATE TABLE IF NOT EXISTS blacklist (
    userid TEXT PRIMARY KEY,
    note TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS watermark (
    key TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS display_names (
    kind TEXT NOT NULL CHECK (kind IN ('room', 'user')),
    value TEXT NOT NULL,
    name TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    created_at INTEGER NOT NULL
);
"""
