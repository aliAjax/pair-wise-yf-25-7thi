"""共享基础：业务错误、时间戳、数据库连接与通用数据访问助手。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_user(conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
    if not user_id:
        raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise BusinessError("用户不存在", 401, "unknown_user")
    return row


def require_role(row: sqlite3.Row, role: str) -> None:
    if row["role"] != role:
        raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")


def write_audit(conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
    )
