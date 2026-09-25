import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore

FINAL_ABSTRACT = "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验与真实部署验证其安全性和性能。"


class FinalizationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _decided_paper(self, decision="minor_revision"):
        paper_id = self.store.submit_paper(
            "alice", "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
        )["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.decide("chair", paper_id, decision, "")
        return paper_id

    def _submit_final(self, paper_id, **overrides):
        payload = {
            "title": "可靠分布式提交协议（修订版）",
            "abstract": FINAL_ABSTRACT,
            "author_order": ["alice", "bob"],
            "format_notes": "已按会议模板排版，PDF 共 12 页。",
        }
        payload.update(overrides)
        return self.store.submit_final("alice", paper_id, **payload)

    def test_submit_final_creates_version_and_locks_submission(self):
        paper_id = self._decided_paper()
        result = self._submit_final(paper_id)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["status"], "pending_chair")
        self.assertEqual(result["changed_fields"], ["title", "author_order"])

        versions = self.store.list_versions("chair", paper_id)
        self.assertEqual([v["kind"] for v in versions], ["submission", "final"])
        self.assertTrue(versions[0]["readonly"])   # 送审版已设为只读
        self.assertFalse(versions[1]["readonly"])
        self.assertEqual(versions[0]["title"], "可靠分布式提交协议")  # 旧版内容保留
        self.assertEqual(versions[1]["title"], "可靠分布式提交协议（修订版）")

        paper = self.store.get_paper("chair", paper_id)
        self.assertEqual(paper["title"], "可靠分布式提交协议（修订版）")
        self.assertEqual(paper["authors"], ["alice", "bob"])
        self.assertEqual(paper["final_status"], "pending_chair")

        with self.assertRaises(BusinessError) as ctx:  # 待核对期间重复提交 -> 409
            self._submit_final(paper_id)
        self.assertEqual(ctx.exception.code, "final_exists")
        self.assertEqual(ctx.exception.status, 409)

    def test_empty_format_notes_returns_to_pending(self):
        paper_id = self._decided_paper()
        with self.assertRaises(BusinessError) as ctx:
            self._submit_final(paper_id, format_notes="   ")
        self.assertEqual(ctx.exception.code, "format_notes_required")
        self.assertEqual(ctx.exception.status, 422)

        with self.assertRaises(BusinessError) as ctx:  # 未生成定稿记录，仍是待提交
            self.store.get_final("chair", paper_id)
        self.assertEqual(ctx.exception.code, "final_not_found")

        result = self._submit_final(paper_id)  # 补充排版说明后可重新提交
        self.assertEqual(result["status"], "pending_chair")

    def test_chair_must_fill_reason_then_seal(self):
        paper_id = self._decided_paper()
        self._submit_final(paper_id)

        with self.assertRaises(BusinessError) as ctx:  # 有变更不填原因 -> 422
            self.store.approve_final("chair", paper_id, "")
        self.assertEqual(ctx.exception.code, "change_reason_required")

        result = self.store.approve_final(
            "chair", paper_id, "作者顺序经全体作者邮件确认，标题按评审意见修订。"
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["signed_by"], "chair")
        self.assertIsNotNone(result["signed_at"])

        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "finalized")
        versions = self.store.list_versions("chair", paper_id)
        self.assertTrue(all(v["readonly"] for v in versions))  # 封版：全部只读

        with self.assertRaises(BusinessError) as ctx:  # 封版后重复提交 -> 409
            self._submit_final(paper_id)
        self.assertEqual(ctx.exception.code, "paper_sealed")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(BusinessError) as ctx:  # 重复签字 -> 409
            self.store.approve_final("chair", paper_id, "再次签字")
        self.assertEqual(ctx.exception.code, "paper_sealed")

        record = self.store.get_final("chair", paper_id)  # 变更原因与签字记录仍可查
        self.assertEqual(record["change_reason"], "作者顺序经全体作者邮件确认，标题按评审意见修订。")
        self.assertEqual(record["signed_by"], "chair")
        self.assertIsNotNone(record["signed_at"])
        self.assertEqual(record["review_version"]["title"], "可靠分布式提交协议")
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("final.submit", actions)
        self.assertIn("final.approve", actions)

    def test_unchanged_final_needs_no_reason(self):
        paper_id = self._decided_paper()
        result = self._submit_final(
            paper_id, title="可靠分布式提交协议", author_order=["alice"]
        )
        self.assertEqual(result["changed_fields"], [])
        approved = self.store.approve_final("chair", paper_id)
        self.assertEqual(approved["status"], "approved")

    def test_final_requires_eligible_decision(self):
        pending = self.store.submit_paper(
            "alice", "未评审的论文标题",
            "这篇论文还没有收到任何评审意见，因此不能提交定稿。",
        )["id"]
        with self.assertRaises(BusinessError) as ctx:
            self._submit_final(pending)
        self.assertEqual(ctx.exception.code, "final_not_allowed")

        rejected = self._decided_paper("reject")
        with self.assertRaises(BusinessError) as ctx:
            self._submit_final(rejected)
        self.assertEqual(ctx.exception.code, "final_not_allowed")

        accepted = self._decided_paper("accept")
        self.assertEqual(self._submit_final(accepted)["status"], "pending_chair")

    def test_permissions_and_double_blind_versions(self):
        paper_id = self._decided_paper()
        with self.assertRaises(BusinessError) as ctx:  # 非本人论文不能提交
            self.store.submit_final(
                "bob", paper_id, "他人论文的定稿标题",
                "这是一段用于占位的摘要内容，至少需要二十个字。",
                ["bob"], "排版说明",
            )
        self.assertEqual(ctx.exception.status, 404)

        self._submit_final(paper_id)
        with self.assertRaises(BusinessError) as ctx:  # 评审人看不到定稿记录
            self.store.get_final("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:  # 其他作者也不行
            self.store.get_final("bob", paper_id)
        self.assertEqual(ctx.exception.status, 403)

        versions = self.store.list_versions("r1", paper_id)  # 评审人双盲视图
        self.assertTrue(versions)
        self.assertTrue(all("authors" not in v for v in versions))


class SchemaMigrationTests(unittest.TestCase):
    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "old.db")
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    load_limit INTEGER NOT NULL DEFAULT 3
                );
                CREATE TABLE papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE paper_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (paper_id, version)
                );
                CREATE TABLE audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER,
                    actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                INSERT INTO users VALUES('alice','Alice 作者','author',0);
                INSERT INTO papers(author_id,title,abstract,created_at)
                    VALUES('alice','旧库标题','旧库摘要内容','2026-01-01T00:00:00+00:00');
                INSERT INTO paper_versions(paper_id,version,content_hash,created_at)
                    VALUES(1,1,'deadbeef','2026-01-01T00:00:00+00:00');
                """
            )
            conn.close()

            store = ReviewStore(db)
            store.init_schema()

            versions = store.list_versions("alice", 1)  # 送审版内容已回填，旧版可查
            self.assertEqual(versions[0]["title"], "旧库标题")
            self.assertEqual(versions[0]["authors"], ["alice"])

            with store.connect() as conn2:  # papers 已允许 finalized 状态
                conn2.execute("UPDATE papers SET status='finalized' WHERE id=1")


if __name__ == "__main__":
    unittest.main()
