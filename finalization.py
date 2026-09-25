"""录用定稿：核对规则与签字记录。

决定为接收或小修后，作者提交最终标题、摘要、作者顺序与排版说明；系统
保留新版本并把送审版设为只读。最终标题或作者顺序与送审版不同时，主席
必须核对并填写变更原因；排版说明为空则退回待提交。主席签字后论文封版，
重复提交返回 409，旧版、变更原因与签字记录始终可查。

版本存储见 versions.py，页面入口见 web/final.html。
"""
from __future__ import annotations

import json
import sqlite3
import threading

from common import BusinessError, connect as db_connect
from common import get_user, require_role, utcnow, write_audit
from versions import VersionStore

ELIGIBLE_DECISIONS = {"accept", "minor_revision"}  # 只有接收或小修才允许定稿
PENDING = "pending_chair"   # 已提交，待主席核对
APPROVED = "approved"       # 主席已验收，论文封版

SCHEMA = """
CREATE TABLE IF NOT EXISTS finalizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
    version INTEGER NOT NULL,
    final_title TEXT NOT NULL,
    final_abstract TEXT NOT NULL,
    author_order TEXT NOT NULL,
    format_notes TEXT NOT NULL,
    changed_fields TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending_chair'
        CHECK (status IN ('pending_chair','approved')),
    change_reason TEXT NOT NULL DEFAULT '',
    signed_by TEXT REFERENCES users(id),
    signed_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""


class FinalizationService:
    """定稿提交、主席核对与封版。每个公开方法使用独立连接。"""

    def __init__(self, db_path: str, versions: VersionStore):
        self.db_path = str(db_path)
        self.versions = versions
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        return db_connect(self.db_path)

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def _record(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM finalizations WHERE paper_id=?", (paper_id,)).fetchone()

    def status_for(self, conn: sqlite3.Connection, paper_id: int) -> str | None:
        row = self._record(conn, paper_id)
        return row["status"] if row else None

    @staticmethod
    def _changed_fields(submission: sqlite3.Row | None, title: str, authors: list[str]) -> list[str]:
        """最终标题或作者顺序与送审版不同时，列入待主席核对的变更项。"""
        if submission is None:
            # 找不到送审版时保守处理，强制主席核对两项变更。
            return ["title", "author_order"]
        changed = []
        if title != submission["title"]:
            changed.append("title")
        if authors != json.loads(submission["authors"]):
            changed.append("author_order")
        return changed

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        return {
            "paper_id": row["paper_id"],
            "version": row["version"],
            "status": row["status"],
            "final_title": row["final_title"],
            "final_abstract": row["final_abstract"],
            "author_order": json.loads(row["author_order"]),
            "format_notes": row["format_notes"],
            "changed_fields": json.loads(row["changed_fields"]),
            "change_reason": row["change_reason"],
            "signed_by": row["signed_by"],
            "signed_at": row["signed_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def submit(self, author_id: str, paper_id: int, title: str, abstract: str,
               author_order: list[str], format_notes: str) -> dict:
        title, abstract = str(title or "").strip(), str(abstract or "").strip()
        format_notes = str(format_notes or "").strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        if not isinstance(author_order, list):
            raise BusinessError("作者顺序必须是非空列表", 422, "invalid_authors")
        authors = [a.strip() for a in author_order if isinstance(a, str) and a.strip()]
        if not authors:
            raise BusinessError("作者顺序不能为空", 422, "invalid_authors")
        if not format_notes:
            # 排版说明为空：不生成定稿记录，论文保持在待提交状态。
            raise BusinessError("排版说明不能为空，定稿已退回待提交", 422, "format_notes_required")
        with self.connect() as conn:
            user = get_user(conn, author_id)
            require_role(user, "author")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["author_id"] != author_id:
                    raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
                decision = conn.execute(
                    "SELECT decision FROM decisions WHERE paper_id=?", (paper_id,)
                ).fetchone()
                if not decision or decision["decision"] not in ELIGIBLE_DECISIONS:
                    raise BusinessError("决定为接收或小修后才能提交定稿", 409, "final_not_allowed")
                existing = self._record(conn, paper_id)
                if existing and existing["status"] == APPROVED:
                    raise BusinessError("论文已封版，不能重复提交定稿", 409, "paper_sealed")
                if existing:
                    raise BusinessError("定稿已提交，等待主席核对", 409, "final_exists")
                submission = self.versions.submission(conn, paper_id)
                changed = self._changed_fields(submission, title, authors)
                rec = self.versions.record(
                    conn, paper_id, "final", title, abstract, authors, format_notes
                )
                self.versions.mark_readonly(conn, paper_id, kind="submission")  # 送审版只读
                conn.execute(
                    """INSERT INTO finalizations(paper_id,version,final_title,final_abstract,
                          author_order,format_notes,changed_fields,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (paper_id, rec["version"], title, abstract,
                     json.dumps(authors, ensure_ascii=False), format_notes,
                     json.dumps(changed, ensure_ascii=False), author_id, utcnow()),
                )
                conn.execute("UPDATE papers SET title=?, abstract=? WHERE id=?", (title, abstract, paper_id))
                write_audit(conn, paper_id, author_id, "final.submit",
                            {"version": rec["version"], "changed_fields": changed})
                return {
                    "paper_id": paper_id,
                    "version": rec["version"],
                    "status": PENDING,
                    "changed_fields": changed,
                    "sha256": rec["sha256"],
                }
            except Exception:
                conn.rollback()
                raise

    def approve(self, chair_id: str, paper_id: int, change_reason: str = "") -> dict:
        change_reason = str(change_reason or "").strip()
        with self.connect() as conn:
            user = get_user(conn, chair_id)
            require_role(user, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._record(conn, paper_id)
                if not row:
                    raise BusinessError("尚未提交定稿", 404, "final_not_found")
                if row["status"] == APPROVED:
                    raise BusinessError("论文已封版，不能重复签字", 409, "paper_sealed")
                changed = json.loads(row["changed_fields"])
                if changed and not change_reason:
                    raise BusinessError(
                        "最终标题或作者顺序与送审版不同，主席必须核对并填写变更原因",
                        422, "change_reason_required",
                    )
                now = utcnow()
                conn.execute(
                    "UPDATE finalizations SET status=?, change_reason=?, signed_by=?, signed_at=? WHERE paper_id=?",
                    (APPROVED, change_reason, chair_id, now, paper_id),
                )
                conn.execute("UPDATE papers SET status='finalized' WHERE id=?", (paper_id,))
                self.versions.mark_readonly(conn, paper_id)  # 封版：全部版本只读
                write_audit(conn, paper_id, chair_id, "final.approve", {
                    "version": row["version"],
                    "changed_fields": changed,
                    "change_reason": change_reason,
                    "signed_by": chair_id,
                })
                return {
                    "paper_id": paper_id,
                    "version": row["version"],
                    "status": APPROVED,
                    "changed_fields": changed,
                    "change_reason": change_reason,
                    "signed_by": chair_id,
                    "signed_at": now,
                }
            except Exception:
                conn.rollback()
                raise

    def get_for_user(self, user_id: str, paper_id: int) -> dict:
        """定稿记录（含变更原因与签字），仅主席与作者本人可查；封版后依然可查。"""
        with self.connect() as conn:
            user = get_user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] != "chair" and paper["author_id"] != user["id"]:
                raise BusinessError("只有主席或论文作者可以查看定稿记录", 403, "forbidden")
            row = self._record(conn, paper_id)
            if not row:
                raise BusinessError("尚未提交定稿", 404, "final_not_found")
            data = self._to_dict(row)
            submission = self.versions.submission(conn, paper_id)
            if submission:
                # 附带送审版快照，供主席核对差异。
                data["review_version"] = {
                    "version": submission["version"],
                    "title": submission["title"],
                    "authors": json.loads(submission["authors"]),
                    "readonly": bool(submission["readonly"]),
                }
            return data
