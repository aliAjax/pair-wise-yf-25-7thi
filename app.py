"""学术会议同行评审系统：标准库 + SQLite 的可运行示例。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import verification
from versioning import VersionStore

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}
FINAL_ELIGIBLE_DECISIONS = {"accept", "minor_revision"}


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()
        self.versions = VersionStore()  # 版本存储与核对规则、页面入口分开维护

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    authors TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn','finalized')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS bids (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed')),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (paper_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS rebuttals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    author_id TEXT NOT NULL REFERENCES users(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
                    note TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES papers(id)
                );
                """
            )
            self._migrate_legacy_papers(conn)
            self.versions.ensure_schema(conn)

    def _migrate_legacy_papers(self, conn: sqlite3.Connection) -> None:
        """旧库补 authors 列；旧的 papers.status CHECK 不含 finalized 时重建表扩展枚举。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "authors" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN authors TEXT NOT NULL DEFAULT '[]'")
            rows = conn.execute("SELECT id, author_id FROM papers").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE papers SET authors=? WHERE id=?",
                    (json.dumps([row["author_id"]]), row["id"]),
                )
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='papers'"
        ).fetchone()[0]
        if "finalized" in sql:
            return
        conn.commit()  # 结束当前事务，重建表需要临时关闭外键约束。
        with self.connect() as migrate:
            migrate.execute("PRAGMA foreign_keys = OFF")
            migrate.execute("BEGIN")
            migrate.executescript(
                """
                CREATE TABLE papers_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    authors TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn','finalized')),
                    created_at TEXT NOT NULL
                );
                INSERT INTO papers_new(id,author_id,title,abstract,authors,status,created_at)
                    SELECT id,author_id,title,abstract,authors,status,created_at FROM papers;
                DROP TABLE papers;
                ALTER TABLE papers_new RENAME TO papers;
                """
            )
        # 重新进入外层 with 块的事务上下文。
        conn.execute("BEGIN")

    def seed(self) -> None:
        self.init_schema()
        users = [
            ("alice", "Alice 作者", "author", 0),
            ("bob", "Bob 作者", "author", 0),
            ("r1", "评审人一号", "reviewer", 3),
            ("r2", "评审人二号", "reviewer", 3),
            ("r3", "评审人三号", "reviewer", 2),
            ("chair", "程序委员会主席", "chair", 0),
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)", users
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _normalize_authors(self, conn: sqlite3.Connection, author_id: str, authors) -> list[str]:
        """校验作者顺序：非空、去重保序、必须都是作者，且提交人在列。"""
        if authors is None:
            authors = [author_id]
        if not isinstance(authors, list) or not authors or not all(isinstance(a, str) and a.strip() for a in authors):
            raise BusinessError("作者顺序必须是非空的用户编号列表", 422, "invalid_authors")
        ordered: list[str] = []
        for raw in authors:
            uid = raw.strip()
            if uid not in ordered:
                ordered.append(uid)
        if author_id not in ordered:
            raise BusinessError("提交人必须包含在作者顺序中", 422, "invalid_authors")
        placeholders = ",".join("?" for _ in ordered)
        rows = conn.execute(f"SELECT id FROM users WHERE role='author' AND id IN ({placeholders})", ordered).fetchall()
        if len(rows) != len(ordered):
            raise BusinessError("作者顺序中包含不存在的作者账号", 422, "invalid_authors")
        return ordered

    def submit_paper(self, user_id: str, title: str, abstract: str, authors: list[str] | None = None) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            ordered = self._normalize_authors(conn, user_id, authors)
            now = utcnow()
            cur = conn.execute(
                "INSERT INTO papers(author_id,title,abstract,authors,created_at) VALUES(?,?,?,?,?)",
                (user_id, title, abstract, json.dumps(ordered, ensure_ascii=False), now),
            )
            paper_id = cur.lastrowid
            snapshot = self.versions.add_snapshot(
                conn,
                paper_id,
                1,
                "submission",
                {"title": title, "abstract": abstract, "authors": ordered, "typesetting": ""},
                now,
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {"version": 1, "sha256": snapshot["content_hash"]})
            return {"id": paper_id, "status": "submitted", "version": 1, "sha256": snapshot["content_hash"], "authors": ordered}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        privileged = viewer["role"] == "chair" or viewer["id"] == paper["author_id"]
        if privileged:
            data["author_id"] = paper["author_id"]
            data["authors"] = self._paper_authors(paper)
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
            data["authors"] = None
        return data

    @staticmethod
    def _paper_authors(paper: sqlite3.Row) -> list[str]:
        try:
            authors = json.loads(paper["authors"] or "[]")
        except json.JSONDecodeError:
            authors = []
        return authors or [paper["author_id"]]

    def _finalization_paper(self, conn: sqlite3.Connection, user: sqlite3.Row, paper_id: int) -> sqlite3.Row:
        """定稿相关接口的统一鉴权：作者只能访问本人论文；评审人不可见。"""
        paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not paper:
            raise BusinessError("论文不存在", 404, "not_found")
        if user["role"] == "author" and paper["author_id"] != user["id"]:
            raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
        if user["role"] == "reviewer":
            raise BusinessError("定稿信息不向评审人开放", 403, "forbidden")
        return paper

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
            elif user["role"] == "author":
                rows = conn.execute("SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.* FROM papers p
                       LEFT JOIN assignments a ON a.paper_id=p.id AND a.reviewer_id=?
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE a.id IS NOT NULL OR b.paper_id IS NOT NULL ORDER BY p.id""",
                    (user_id, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, user_id, paper_id, user_id),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            return self._paper_view(conn, paper, user)

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            try:
                conn.execute(
                    "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("利益冲突已登记", 409, "conflict_exists")
            self._audit(conn, paper_id, chair_id, "conflict.add", {"reviewer_id": reviewer_id, "reason": reason.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "reason": reason.strip()}

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,interest,note,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {"interest": interest, "note": note.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "interest": interest}

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                load = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                    (reviewer_id,),
                ).fetchone()[0]
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                try:
                    cur = conn.execute(
                        "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
                        (paper_id, reviewer_id, utcnow(), utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                assignment_id = cur.lastrowid
                self._audit(conn, paper_id, chair_id, "assignment.invite", {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
            self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
            return {"id": assignment_id, "status": status}

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (score, text.strip(), utcnow(), assignment_id),
            )
            self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {"assignment_id": assignment_id, "score": score})
            return {"id": assignment_id, "status": "completed", "score": score}

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
                if completed < 2:
                    raise BusinessError("至少需要两份已完成评审才能作出决定", 409, "insufficient_reviews")
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                if decision in FINAL_ELIGIBLE_DECISIONS:
                    # 接收或小修后进入定稿流程：作者提交，定稿前留空版本号。
                    self.versions.upsert_finalization(
                        conn, paper_id, status="pending_submission", version=0,
                        title_changed=0, abstract_changed=0, authors_changed=0,
                        change_reason="", verified_by=None, verified_at=None,
                        accepted_by=None, accepted_at=None, updated_at=utcnow(),
                    )
                self._audit(conn, paper_id, chair_id, "decision.record", {"decision": decision, "note": note.strip()})
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def _final_view(self, conn: sqlite3.Connection, paper: sqlite3.Row) -> dict:
        decision_row = conn.execute(
            "SELECT decision FROM decisions WHERE paper_id=?", (paper["id"],)
        ).fetchone()
        decision = decision_row["decision"] if decision_row else None
        if decision not in FINAL_ELIGIBLE_DECISIONS:
            # 拒稿/大修不开放定稿；尚未决定时也没有定稿任务。
            return {"paper_id": paper["id"], "decision": decision, "status": None}
        record = self.versions.get_finalization(conn, paper["id"])
        status = (record or {}).get("status", "pending_submission")
        view: dict = {
            "paper_id": paper["id"],
            "decision": decision,
            "status": status,
        }
        if record and record.get("version"):
            view.update(
                {
                    "version": record["version"],
                    "changes": {
                        "title_changed": record["title_changed"],
                        "abstract_changed": record["abstract_changed"],
                        "authors_changed": record["authors_changed"],
                    },
                    "change_reason": record.get("change_reason") or "",
                    "verified_by": record.get("verified_by"),
                    "verified_at": record.get("verified_at"),
                    "accepted_by": record.get("accepted_by"),
                    "accepted_at": record.get("accepted_at"),
                    "camera_ready": self.versions.get_snapshot(conn, paper["id"], record["version"]),
                    "submission": self.versions.submission_snapshot(conn, paper["id"]),
                }
            )
        else:
            view["submission"] = self.versions.submission_snapshot(conn, paper["id"])
        return view

    def submit_final(
        self,
        author_id: str,
        paper_id: int,
        title: str,
        abstract: str,
        authors: list[str] | None,
        typesetting: str,
    ) -> dict:
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = self._finalization_paper(conn, author, paper_id)
            decision_row = conn.execute(
                "SELECT decision FROM decisions WHERE paper_id=?", (paper_id,)
            ).fetchone()
            if not decision_row or decision_row["decision"] not in FINAL_ELIGIBLE_DECISIONS:
                raise BusinessError("只有接收或小修的论文才能提交定稿", 409, "final_not_eligible")
            record = self.versions.get_finalization(conn, paper_id)
            current = (record or {}).get("status", "pending_submission")
            if current == "finalized":
                raise BusinessError("论文已封版，不能重复提交", 409, "paper_finalized")
            if current in {"pending_verification", "pending_acceptance"}:
                raise BusinessError("定稿已提交，等待主席核对验收", 409, "final_already_submitted")

            # 状态确认仍处于待提交后，再校验内容；退回时不产生新版本。
            title, abstract, typesetting = title.strip(), abstract.strip(), typesetting.strip()
            if len(title) < 3 or len(abstract) < 20:
                raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
            if not typesetting:
                raise BusinessError("排版说明不能为空", 422, "typesetting_required")
            ordered = self._normalize_authors(conn, author_id, authors)
            submission = self.versions.submission_snapshot(conn, paper_id)
            if not submission:
                raise BusinessError("送审版快照缺失，无法核对", 409, "submission_snapshot_missing")
            now = utcnow()
            version = self.versions.next_version(conn, paper_id)
            snapshot = self.versions.add_snapshot(
                conn,
                paper_id,
                version,
                "camera_ready",
                {"title": title, "abstract": abstract, "authors": ordered, "typesetting": typesetting},
                now,
            )
            # 新版本保留后，送审版立即冻结为只读，不能再被覆盖。
            self.versions.lock_submission(conn, paper_id)
            changes = verification.diff_snapshots(submission, snapshot)
            needs_check = verification.needs_chair_verification(changes)
            status = "pending_verification" if needs_check else "pending_acceptance"
            self.versions.upsert_finalization(
                conn,
                paper_id,
                status=status,
                version=version,
                title_changed=int(changes["title_changed"]),
                abstract_changed=int(changes["abstract_changed"]),
                authors_changed=int(changes["authors_changed"]),
                change_reason="",
                verified_by=None,
                verified_at=None,
                accepted_by=None,
                accepted_at=None,
                updated_at=now,
            )
            # 定稿成为对外展示的当前标题/摘要/作者顺序；旧版仍在 paper_versions 可查。
            conn.execute(
                "UPDATE papers SET title=?,abstract=?,authors=? WHERE id=?",
                (title, abstract, json.dumps(ordered, ensure_ascii=False), paper_id),
            )
            self._audit(
                conn,
                paper_id,
                author_id,
                "final.submit",
                {"version": version, "changes": changes, "needs_chair_verification": needs_check},
            )
            return self._final_view(conn, paper)

    def verify_final(self, chair_id: str, paper_id: int, change_reason: str) -> dict:
        """主席核对标题/作者顺序变更并填写变更原因。"""
        change_reason = change_reason.strip()
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            record = self.versions.get_finalization(conn, paper_id)
            if not record or record["status"] == "pending_submission":
                raise BusinessError("作者尚未提交定稿", 409, "final_not_submitted")
            if record["status"] == "finalized":
                raise BusinessError("论文已封版", 409, "paper_finalized")
            changes = {
                "title_changed": record["title_changed"],
                "abstract_changed": record["abstract_changed"],
                "authors_changed": record["authors_changed"],
            }
            if not verification.needs_chair_verification(changes):
                raise BusinessError("标题和作者顺序未变更，无需核对", 409, "verification_not_required")
            if verification.change_reason_required(changes, change_reason):
                raise BusinessError("标题或作者顺序已变更，必须填写变更原因", 422, "change_reason_required")
            now = utcnow()
            self.versions.upsert_finalization(conn, paper_id, status="pending_acceptance",
                                              change_reason=change_reason, verified_by=chair_id,
                                              verified_at=now, updated_at=now)
            self._audit(
                conn, paper_id, chair_id, "final.verify",
                {"version": record["version"], "change_reason": change_reason},
            )
            return self._final_view(conn, paper)

    def accept_final(self, chair_id: str, paper_id: int) -> dict:
        """主席验收：无变更直接验收；有变更必须已核对。验收后封版。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            record = self.versions.get_finalization(conn, paper_id)
            if not record or record["status"] == "pending_submission":
                raise BusinessError("作者尚未提交定稿", 409, "final_not_submitted")
            if record["status"] == "finalized":
                raise BusinessError("论文已封版，不能重复验收", 409, "paper_finalized")
            if record["status"] != "pending_acceptance":
                raise BusinessError("标题或作者顺序变更尚未核对", 409, "verification_required")
            now = utcnow()
            self.versions.upsert_finalization(conn, paper_id, status="finalized",
                                              accepted_by=chair_id, accepted_at=now, updated_at=now)
            conn.execute("UPDATE papers SET status='finalized' WHERE id=?", (paper_id,))
            self._audit(conn, paper_id, chair_id, "final.accept", {"version": record["version"]})
            return self._final_view(conn, paper)

    def get_final(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = self._finalization_paper(conn, user, paper_id)
            return self._final_view(conn, paper)

    def list_versions(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._finalization_paper(conn, user, paper_id)
            items = self.versions.list_versions(conn, paper_id)
            return {"paper_id": paper_id, "items": items}

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "AcademicReview/1.0"

    def _store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")

    def _user_id(self) -> str:
        return self.headers.get("X-User-Id", "")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            html = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if method == "GET" and path == "/final":
            # 定稿页面入口单独维护（web/final.html），与版本存储、核对规则解耦。
            html = (BASE_DIR / "web" / "final.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "api":
            raise BusinessError("接口不存在", 404, "not_found")
        if parts == ["api", "papers"] and method == "GET":
            return self._send(200, {"items": store.list_papers(self._user_id())})
        if parts == ["api", "papers"] and method == "POST":
            data = self._body()
            return self._send(201, store.submit_paper(
                self._user_id(), data.get("title", ""), data.get("abstract", ""), data.get("authors")))
        if len(parts) >= 3 and parts[:2] == ["api", "papers"]:
            paper_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_paper(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "bids" and method == "POST":
                data = self._body()
                return self._send(201, store.bid(self._user_id(), paper_id, data.get("interest", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "conflicts" and method == "POST":
                data = self._body()
                return self._send(201, store.add_conflict(self._user_id(), paper_id, data.get("reviewer_id", ""), data.get("reason", "")))
            if len(parts) == 4 and parts[3] == "assignments" and method == "POST":
                data = self._body()
                return self._send(201, store.assign(self._user_id(), paper_id, data.get("reviewer_id", "")))
            if len(parts) == 4 and parts[3] == "rebuttal" and method == "POST":
                data = self._body()
                return self._send(201, store.submit_rebuttal(self._user_id(), paper_id, data.get("content", "")))
            if len(parts) == 4 and parts[3] == "decision" and method == "POST":
                data = self._body()
                return self._send(201, store.decide(self._user_id(), paper_id, data.get("decision", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.history(self._user_id(), paper_id)})
            if len(parts) == 4 and parts[3] == "final" and method == "GET":
                return self._send(200, store.get_final(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "final" and method == "POST":
                data = self._body()
                return self._send(201, store.submit_final(
                    self._user_id(), paper_id, data.get("title", ""), data.get("abstract", ""),
                    data.get("authors"), data.get("typesetting", "")))
            if len(parts) == 5 and parts[3] == "final" and parts[4] == "verify" and method == "POST":
                data = self._body()
                return self._send(200, store.verify_final(self._user_id(), paper_id, data.get("change_reason", "")))
            if len(parts) == 5 and parts[3] == "final" and parts[4] == "accept" and method == "POST":
                return self._send(200, store.accept_final(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "versions" and method == "GET":
                return self._send(200, store.list_versions(self._user_id(), paper_id))
        if len(parts) == 4 and parts[:2] == ["api", "assignments"] and method == "POST":
            assignment_id = int(parts[2])
            data = self._body()
            if parts[3] == "respond":
                return self._send(200, store.respond_assignment(self._user_id(), assignment_id, bool(data.get("accepted"))))
            if parts[3] == "review":
                return self._send(201, store.submit_review(self._user_id(), assignment_id, data.get("score"), data.get("text", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError:
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: ReviewStore):
        self.store = store
        super().__init__(address, ReviewHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="学术会议同行评审系统")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--init", action="store_true", help="初始化数据库")
    parser.add_argument("--seed", action="store_true", help="写入演示用户")
    parser.add_argument("--no-init", action="store_true", help="启动时不自动初始化")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ReviewStore(args.db)
    if args.init or args.seed or not args.no_init:
        store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = ReviewServer(("127.0.0.1", args.port), store)
    print(f"评审系统运行于 http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
