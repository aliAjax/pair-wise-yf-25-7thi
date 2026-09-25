"""版本存储：``paper_versions`` 表的结构、迁移与读写。

只负责论文内容版本（送审版、定稿版）的持久化，不包含录用定稿的核对
规则（见 finalization.py）与页面入口（见 web/final.html）。
除 init_schema 外，函数均接收调用方连接，以便并入调用方事务。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from common import connect as db_connect
from common import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    version INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'submission' CHECK (kind IN ('submission','final')),
    title TEXT NOT NULL DEFAULT '',
    abstract TEXT NOT NULL DEFAULT '',
    authors TEXT NOT NULL DEFAULT '[]',
    format_notes TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    readonly INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, version)
);
"""

# 旧版本库缺少的列，按 (列名, DDL) 顺序补齐。
_COLUMN_MIGRATIONS = [
    ("kind", "TEXT NOT NULL DEFAULT 'submission'"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("abstract", "TEXT NOT NULL DEFAULT ''"),
    ("authors", "TEXT NOT NULL DEFAULT '[]'"),
    ("format_notes", "TEXT NOT NULL DEFAULT ''"),
    ("readonly", "INTEGER NOT NULL DEFAULT 0"),
]


class VersionStore:
    """论文内容版本的写入、只读标记与查询。"""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        return db_connect(self.db_path)

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_versions)")}
        for name, ddl in _COLUMN_MIGRATIONS:
            if name not in cols:
                conn.execute(f"ALTER TABLE paper_versions ADD COLUMN {name} {ddl}")
        # 旧数据只存了哈希：从 papers 回填送审版内容，保证旧版仍可查。
        missing = conn.execute("SELECT id, paper_id FROM paper_versions WHERE title=''").fetchall()
        for row in missing:
            paper = conn.execute(
                "SELECT title, abstract, author_id FROM papers WHERE id=?", (row["paper_id"],)
            ).fetchone()
            if paper:
                conn.execute(
                    "UPDATE paper_versions SET title=?, abstract=?, authors=? WHERE id=?",
                    (paper["title"], paper["abstract"],
                     json.dumps([paper["author_id"]], ensure_ascii=False), row["id"]),
                )

    @staticmethod
    def _digest(title: str, abstract: str, authors: list[str]) -> str:
        payload = json.dumps([title, abstract, authors], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def next_version(self, conn: sqlite3.Connection, paper_id: int) -> int:
        return conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM paper_versions WHERE paper_id=?", (paper_id,)
        ).fetchone()[0]

    def record(self, conn: sqlite3.Connection, paper_id: int, kind: str, title: str,
               abstract: str, authors: list[str], format_notes: str = "") -> dict:
        version = self.next_version(conn, paper_id)
        digest = self._digest(title, abstract, authors)
        conn.execute(
            """INSERT INTO paper_versions(paper_id,version,kind,title,abstract,authors,
                  format_notes,content_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (paper_id, version, kind, title, abstract,
             json.dumps(authors, ensure_ascii=False), format_notes, digest, utcnow()),
        )
        return {"version": version, "sha256": digest}

    def submission(self, conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
        """送审版（第 1 版）。"""
        return conn.execute(
            "SELECT * FROM paper_versions WHERE paper_id=? AND kind='submission' ORDER BY version LIMIT 1",
            (paper_id,),
        ).fetchone()

    def latest(self, conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM paper_versions WHERE paper_id=? ORDER BY version DESC LIMIT 1",
            (paper_id,),
        ).fetchone()

    def list(self, conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM paper_versions WHERE paper_id=? ORDER BY version", (paper_id,)
        ).fetchall()

    def mark_readonly(self, conn: sqlite3.Connection, paper_id: int, kind: str | None = None) -> None:
        """把版本设为只读；kind 为 None 时封版该论文的全部版本。"""
        if kind is None:
            conn.execute("UPDATE paper_versions SET readonly=1 WHERE paper_id=?", (paper_id,))
        else:
            conn.execute(
                "UPDATE paper_versions SET readonly=1 WHERE paper_id=? AND kind=?", (paper_id, kind)
            )

    @staticmethod
    def to_dict(row: sqlite3.Row, include_authors: bool = True) -> dict:
        data = {
            "version": row["version"],
            "kind": row["kind"],
            "title": row["title"],
            "abstract": row["abstract"],
            "format_notes": row["format_notes"],
            "readonly": bool(row["readonly"]),
            "sha256": row["content_hash"],
            "created_at": row["created_at"],
        }
        if include_authors:
            data["authors"] = json.loads(row["authors"])
        return data
