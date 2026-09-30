"""SQLite 连接管理与建表初始化。

时间约定：所有时间统一以 UTC、定长字符串 ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` 存储，
因此字典序即时间序，便于与左闭右开有效期做字符串比较。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings

# versions 表允许的事件类型（每次新增写操作需同步登记）
VERSION_EVENT_TYPES = (
    "cert_issue",
    "cert_correct",
    "cert_revoke",
    "reading_submit",
    "reading_adjudicate",
)

_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS versions (
    version                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type             TEXT NOT NULL CHECK (event_type IN ('cert_issue','cert_correct','cert_revoke','reading_submit','reading_adjudicate')),
    request_id             TEXT NOT NULL UNIQUE,
    request_payload_json   TEXT NOT NULL,
    fingerprint            TEXT NOT NULL,
    response_json          TEXT,
    created_at             TEXT NOT NULL
)
"""

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 全局单调版本流水：每次成功变更产生一条记录，version 由 INTEGER PRIMARY KEY 自增
{_VERSIONS_DDL};

-- 证书主档：每个证书编号一行，保存当前生命周期状态
CREATE TABLE IF NOT EXISTS certificates (
    certificate_no        TEXT PRIMARY KEY,
    current_version_seq   INTEGER NOT NULL DEFAULT 1,
    is_revoked            INTEGER NOT NULL DEFAULT 0,
    revoked_at_version    INTEGER REFERENCES versions(version),
    revoked_reason        TEXT,
    created_at_version    INTEGER NOT NULL REFERENCES versions(version),
    updated_at_version    INTEGER NOT NULL REFERENCES versions(version)
);

-- 证书内容流水（不可变）：签发一条；每次纠正再追加一条
CREATE TABLE IF NOT EXISTS certificate_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_no      TEXT NOT NULL REFERENCES certificates(certificate_no),
    version_seq         INTEGER NOT NULL,
    device_no           TEXT NOT NULL,
    valid_from          TEXT NOT NULL,
    valid_to            TEXT NOT NULL,
    min_value           REAL NOT NULL,
    max_value           REAL NOT NULL,
    created_at_version  INTEGER NOT NULL REFERENCES versions(version),
    superseded_at_version INTEGER,  -- 被新纠正版本失效的版本；撤销亦置位
    UNIQUE (certificate_no, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_certver_device_active
    ON certificate_versions(device_no, valid_from, valid_to);

-- 读数（不可变，只能追加）
CREATE TABLE IF NOT EXISTS readings (
    reading_no          TEXT PRIMARY KEY,
    device_no           TEXT NOT NULL,
    measured_at         TEXT NOT NULL,
    value               REAL NOT NULL,
    submitted_at_version INTEGER NOT NULL REFERENCES versions(version),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_device_time ON readings(device_no, measured_at);

-- 读数人工选证裁决（只追加）：对处于 review 的读数人工指定候选证书版本。
-- 是否仍有效不持久化：以后续证书纠正/撤销事实动态计算，
-- 旧版本仍可查到原始裁决；取证包按版本视角投影，内容与摘要恒定。
CREATE TABLE IF NOT EXISTS reading_adjudications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_no            TEXT NOT NULL REFERENCES readings(reading_no),
    version               INTEGER NOT NULL UNIQUE REFERENCES versions(version),
    request_id            TEXT NOT NULL UNIQUE,
    base_version          INTEGER NOT NULL,
    certificate_no        TEXT NOT NULL,
    certificate_version_seq INTEGER NOT NULL,
    reason                TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    FOREIGN KEY (certificate_no, certificate_version_seq)
        REFERENCES certificate_versions(certificate_no, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_adjudications_reading_version
    ON reading_adjudications(reading_no, version);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    # 允许父目录在容器卷场景下自动创建
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=get_settings().busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(get_settings().busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.isolation_level = None  # 事务边界完全由代码显式控制
    return conn


def _migrate_versions_check(conn: sqlite3.Connection) -> None:
    """旧库的 versions CHECK 不含 reading_adjudicate 时，重建表以扩展约束。

    历史行原样搬移，version 自增序列保持不变；整个过程关闭外键并在
    一个事务内完成，失败回滚不影响既有数据。
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'"
    ).fetchone()
    if ddl is not None and "reading_adjudicate" in (ddl["sql"] or ""):
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE versions RENAME TO versions_legacy")
        conn.execute(_VERSIONS_DDL)
        conn.execute(
            "INSERT INTO versions (version, event_type, request_id,"
            " request_payload_json, fingerprint, response_json, created_at)"
            " SELECT version, event_type, request_id, request_payload_json,"
            " fingerprint, response_json, created_at FROM versions_legacy"
        )
        seq = conn.execute("SELECT COALESCE(MAX(version), 0) AS m FROM versions").fetchone()["m"]
        # sqlite_sequence 是无约束的内部 rowid 表，先删后插以恢复自增起点
        conn.execute("DELETE FROM sqlite_sequence WHERE name='versions'")
        if seq:
            conn.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('versions', ?)", (seq,)
            )
        conn.execute("DROP TABLE versions_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db() -> None:
    """创建表结构并初始化数据库（幂等，可重复执行）。"""
    conn = _connect(get_settings().db_path)
    try:
        # 先迁移旧库再执行全量建表脚本，保证新增表与约束就位
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='versions'"
        ).fetchone()
        if existed is not None:
            _migrate_versions_check(conn)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级只读连接。"""
    conn = _connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """请求级一致性只读快照事务（WAL deferred）。

    事务内所有 SELECT 共享同一读快照：事务开始后其他连接的提交
    （新读数提交、证书纠正 / 撤销）在本事务内一律不可见，
    保证一次取证包生成过程读到的是同一个数据库状态。
    """
    conn = _connect(get_settings().db_path)
    try:
        conn.execute("BEGIN")  # 延迟事务，首次 SELECT 时确立 WAL 读快照
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化写事务：BEGIN IMMEDIATE 立刻获取 RESERVED 锁，
    避免并发写在升级锁时发生死锁；异常自动回滚。"""
    conn = _connect(get_settings().db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
