"""SQLite 访问层（自 gateway/db.py 原样移植）。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SCHEMA_SQL


@dataclass(frozen=True)
class WriteResult:
    """一次写入的结果：rowcount 用于判断 INSERT OR IGNORE 是否真的插入。"""

    rowcount: int
    lastrowid: int | None


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._read_lock = threading.Lock()
        self._read_conn: sqlite3.Connection | None = None

    def _writer(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def _conn_for_read(self) -> sqlite3.Connection:
        if self._read_conn is None:
            conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            self._read_conn = conn
        return self._read_conn

    def ensure_schema(self) -> None:
        with self._lock:
            self._writer().executescript(SCHEMA_SQL)

    def _execute_sync(self, sql: str, params: Sequence[Any]) -> WriteResult:
        with self._lock:
            cursor = self._writer().execute(sql, tuple(params))
            return WriteResult(rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> WriteResult:
        return await asyncio.to_thread(self._execute_sync, sql, list(params))

    def execute_now(self, sql: str, params: Sequence[Any] = ()) -> WriteResult:
        """同步写，供测试与启动期脚本使用；运行时路径一律走 execute。"""
        return self._execute_sync(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._read_lock:
            return list(self._conn_for_read().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._read_lock:
            return self._conn_for_read().execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        with self._read_lock:
            if self._read_conn is not None:
                self._read_conn.close()
                self._read_conn = None


def now_ms() -> int:
    import time

    return int(time.time() * 1000)
