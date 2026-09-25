"""版本存储：论文快照、送审版只读锁定与定稿记录的持久层。

只负责存取，不负责权限和状态机编排（在 app.py）与核对规则（在 verification.py）。
所有方法接收一个由调用方控制事务的 sqlite 连接。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

CONTENT_FIELDS = ("title", "abstract", "authors", "typesetting")

# 定稿状态：待提交 → 待核对/待验收 → 已封版。
FINAL_STATUS = ("pending_submission", "pending_verification", "pending_acceptance", "finalized")


def content_hash(snapshot: dict[str, Any]) -> str:
    import hashlib

    payload = "\n".join(
        json.dumps(snapshot.get(key, ""), ensure_ascii=False, sort_keys=True) for key in CONTENT_FIELDS
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class VersionStore:
    """管理 paper_versions 与 finalizations 两张表的结构和读写。"""

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL REFERENCES papers(id),
                version INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'submission'
                    CHECK (kind IN ('submission','camera_ready')),
                title TEXT NOT NULL DEFAULT '',
                abstract TEXT NOT NULL DEFAULT '',
                authors TEXT NOT NULL DEFAULT '[]',
                typesetting TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE (paper_id, version)
            );
            CREATE TABLE IF NOT EXISTS finalizations (
                paper_id INTEGER PRIMARY KEY REFERENCES papers(id),
                status TEXT NOT NULL DEFAULT 'pending_submission'
                    CHECK (status IN ('pending_submission','pending_verification','pending_acceptance','finalized')),
                version INTEGER NOT NULL DEFAULT 0,
                title_changed INTEGER NOT NULL DEFAULT 0,
                abstract_changed INTEGER NOT NULL DEFAULT 0,
                authors_changed INTEGER NOT NULL DEFAULT 0,
                change_reason TEXT NOT NULL DEFAULT '',
                verified_by TEXT REFERENCES users(id),
                verified_at TEXT,
                accepted_by TEXT REFERENCES users(id),
                accepted_at TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._migrate_legacy_schema(conn)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """为旧库补列、回填历史快照；papers.status 的 CHECK 扩展由 app 层完成。"""
        version_cols = self._columns(conn, "paper_versions")
        if version_cols and "kind" not in version_cols:
            conn.executescript(
                """
                ALTER TABLE paper_versions ADD COLUMN kind TEXT NOT NULL DEFAULT 'submission';
                ALTER TABLE paper_versions ADD COLUMN title TEXT NOT NULL DEFAULT '';
                ALTER TABLE paper_versions ADD COLUMN abstract TEXT NOT NULL DEFAULT '';
                ALTER TABLE paper_versions ADD COLUMN authors TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE paper_versions ADD COLUMN typesetting TEXT NOT NULL DEFAULT '';
                ALTER TABLE paper_versions ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;
                """
            )
            rows = conn.execute("SELECT paper_id, version, created_at FROM paper_versions").fetchall()
            for row in rows:
                paper = conn.execute("SELECT title, abstract, author_id FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
                if paper:
                    conn.execute(
                        "UPDATE paper_versions SET title=?,abstract=?,authors=? WHERE paper_id=? AND version=?",
                        (paper["title"], paper["abstract"], json.dumps([paper["author_id"]]), row["paper_id"], row["version"]),
                    )
        if not self._columns(conn, "finalizations"):
            return
        # 全新库无需回填；旧库若已存在 finalizations 则保持其结构。
        conn.execute(
            """INSERT OR IGNORE INTO finalizations(paper_id,status,updated_at)
               SELECT id,'pending_submission',created_at FROM papers"""
        )

    def next_version(self, conn: sqlite3.Connection, paper_id: int) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version),0) FROM paper_versions WHERE paper_id=?", (paper_id,)).fetchone()
        return (row[0] or 0) + 1

    def get_snapshot(self, conn: sqlite3.Connection, paper_id: int, version: int) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM paper_versions WHERE paper_id=? AND version=?", (paper_id, version)
        ).fetchone()
        return self._snapshot(row) if row else None

    def submission_snapshot(self, conn: sqlite3.Connection, paper_id: int) -> dict[str, Any] | None:
        """送审版：被锁定的旧版优先，否则取最早的送审快照。"""
        row = conn.execute(
            """SELECT * FROM paper_versions WHERE paper_id=? AND kind='submission'
               ORDER BY locked DESC, version ASC LIMIT 1""",
            (paper_id,),
        ).fetchone()
        return self._snapshot(row) if row else None

    def list_versions(self, conn: sqlite3.Connection, paper_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM paper_versions WHERE paper_id=? ORDER BY version", (paper_id,)
        ).fetchall()
        return [self._snapshot(row) for row in rows]

    def lock_submission(self, conn: sqlite3.Connection, paper_id: int) -> int:
        """把送审版冻结为只读，返回受影响的版本号数。"""
        cur = conn.execute(
            "UPDATE paper_versions SET locked=1 WHERE paper_id=? AND kind='submission' AND locked=0",
            (paper_id,),
        )
        return cur.rowcount

    def add_snapshot(self, conn: sqlite3.Connection, paper_id: int, version: int, kind: str, snapshot: dict[str, Any], created_at: str) -> dict[str, Any]:
        digest = content_hash(snapshot)
        conn.execute(
            """INSERT INTO paper_versions
                   (paper_id,version,kind,title,abstract,authors,typesetting,content_hash,locked,created_at)
               VALUES(?,?,?,?,?,?,?,?,0,?)""",
            (
                paper_id,
                version,
                kind,
                snapshot.get("title", ""),
                snapshot.get("abstract", ""),
                json.dumps(snapshot.get("authors", []), ensure_ascii=False),
                snapshot.get("typesetting", ""),
                digest,
                created_at,
            ),
        )
        saved = self.get_snapshot(conn, paper_id, version)
        assert saved is not None
        return saved

    def get_finalization(self, conn: sqlite3.Connection, paper_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM finalizations WHERE paper_id=?", (paper_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        for field in ("title_changed", "abstract_changed", "authors_changed"):
            data[field] = bool(data[field])
        return data

    def upsert_finalization(self, conn: sqlite3.Connection, paper_id: int, **fields: Any) -> None:
        fields["paper_id"] = paper_id
        cols = ",".join(fields)
        placeholders = ",".join(f":{key}" for key in fields)
        conn.execute(
            f"""INSERT INTO finalizations({cols}) VALUES({placeholders})
                ON CONFLICT(paper_id) DO UPDATE SET {",".join(f"{k}=excluded.{k}" for k in fields if k != "paper_id")}""",
            fields,
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["authors"] = json.loads(data.get("authors") or "[]")
        except json.JSONDecodeError:
            data["authors"] = []
        data["locked"] = bool(data["locked"])
        return data
