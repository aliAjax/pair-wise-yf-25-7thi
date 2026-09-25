import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore
from verification import diff_snapshots, needs_chair_verification


class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _decide(self, decision="accept", author="alice"):
        paper_id = self.store.submit_paper(
            author, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            authors=[author, "bob"] if author == "alice" else [author],
        )["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 4, "实验充分，写作清楚，小问题见意见。")
        self.store.decide("chair", paper_id, decision)
        return paper_id

    def test_no_changes_goes_straight_to_acceptance_and_seals(self):
        paper_id = self._decide()
        info = self.store.get_final("alice", paper_id)
        self.assertEqual(info["status"], "pending_submission")

        result = self.store.submit_final(
            "alice", paper_id, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            ["alice", "bob"], "终稿使用 10pt 双栏，页码连续。",
        )
        self.assertEqual(result["status"], "pending_acceptance")
        self.assertFalse(result["changes"]["title_changed"])
        self.assertFalse(result["changes"]["authors_changed"])
        self.assertFalse(result["camera_ready"]["locked"])

        accepted = self.store.accept_final("chair", paper_id)
        self.assertEqual(accepted["status"], "finalized")
        self.assertEqual(self.store.get_paper("alice", paper_id)["status"], "finalized")

    def test_title_change_requires_reason_before_acceptance(self):
        paper_id = self._decide()
        result = self.store.submit_final(
            "alice", paper_id, "可靠分布式原子提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            ["alice", "bob"], "终稿使用 10pt 双栏。",
        )
        self.assertEqual(result["status"], "pending_verification")
        self.assertTrue(result["changes"]["title_changed"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.accept_final("chair", paper_id)
        self.assertEqual(ctx.exception.code, "verification_required")
        verified = self.store.verify_final("chair", paper_id, "根据评审意见更准确地体现原子提交贡献。")
        self.assertEqual(verified["status"], "pending_acceptance")
        self.assertEqual(verified["verified_by"], "chair")
        self.store.accept_final("chair", paper_id)

    def test_author_order_change_requires_reason(self):
        paper_id = self._decide()
        result = self.store.submit_final(
            "alice", paper_id, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            ["bob", "alice"], "终稿使用 10pt 双栏。",
        )
        self.assertTrue(result["changes"]["authors_changed"])
        self.assertEqual(result["status"], "pending_verification")
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_final("chair", paper_id, "   ")
        self.assertEqual(ctx.exception.code, "change_reason_required")
        self.store.verify_final("chair", paper_id, "按实际贡献调整，Bob 完成了主体实现。")
        self.store.accept_final("chair", paper_id)

    def test_abstract_only_change_skips_chair_verification(self):
        paper_id = self._decide()
        result = self.store.submit_final(
            "alice", paper_id, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠原子提交协议，并给出更充分的故障注入实验。",
            ["alice", "bob"], "终稿使用 10pt 双栏。",
        )
        self.assertTrue(result["changes"]["abstract_changed"])
        self.assertEqual(result["status"], "pending_acceptance")
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_final("chair", paper_id, "不需要")
        self.assertEqual(ctx.exception.code, "verification_not_required")
        self.store.accept_final("chair", paper_id)

    def test_empty_typesetting_is_rejected_without_snapshot(self):
        paper_id = self._decide()
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["alice", "bob"], "   ",
            )
        self.assertEqual(ctx.exception.code, "typesetting_required")
        info = self.store.get_final("alice", paper_id)
        self.assertEqual(info["status"], "pending_submission")
        self.assertNotIn("camera_ready", info)
        versions = self.store.list_versions("alice", paper_id)["items"]
        self.assertEqual(len(versions), 1)

    def test_resubmit_after_seal_returns_409(self):
        paper_id = self._decide()
        self.store.submit_final(
            "alice", paper_id, "可靠分布式提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            ["alice", "bob"], "终稿使用 10pt 双栏。",
        )
        self.store.accept_final("chair", paper_id)
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["alice", "bob"], "再次提交。",
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "paper_finalized")
        with self.assertRaises(BusinessError) as ctx:
            self.store.accept_final("chair", paper_id)
        self.assertEqual(ctx.exception.status, 409)

    def test_submitted_version_is_readonly_and_history_remains(self):
        paper_id = self._decide()
        self.store.submit_final(
            "alice", paper_id, "可靠分布式原子提交协议",
            "本文提出一种用于弱网环境的可靠原子提交协议，并给出更充分的故障注入实验。",
            ["bob", "alice"], "终稿使用 10pt 双栏。",
        )
        self.store.verify_final("chair", paper_id, "标题与作者顺序按贡献调整。")
        self.store.accept_final("chair", paper_id)

        versions = self.store.list_versions("chair", paper_id)["items"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        v1, v2 = versions
        self.assertTrue(v1["locked"])
        self.assertEqual(v1["title"], "可靠分布式提交协议")
        self.assertEqual(v1["authors"], ["alice", "bob"])
        self.assertEqual(v1["kind"], "submission")
        self.assertFalse(v2["locked"])
        self.assertEqual(v2["title"], "可靠分布式原子提交协议")
        self.assertEqual(v2["authors"], ["bob", "alice"])
        self.assertEqual(v2["typesetting"], "终稿使用 10pt 双栏。")

        final = self.store.get_final("alice", paper_id)
        self.assertEqual(final["status"], "finalized")
        self.assertEqual(final["submission"]["version"], 1)
        self.assertEqual(final["camera_ready"]["version"], 2)
        self.assertEqual(final["change_reason"], "标题与作者顺序按贡献调整。")
        self.assertEqual(final["accepted_by"], "chair")

        history = self.store.history("alice", paper_id)
        actions = [row["action"] for row in history]
        self.assertEqual(
            [a for a in actions if a.startswith("final.")],
            ["final.submit", "final.verify", "final.accept"],
        )

    def test_rejected_paper_cannot_submit_final(self):
        paper_id = self._decide(decision="reject")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["alice", "bob"], "终稿排版。",
            )
        self.assertEqual(ctx.exception.code, "final_not_eligible")
        info = self.store.get_final("chair", paper_id)
        self.assertIsNone(info["status"])

    def test_duplicate_submission_while_pending_returns_409(self):
        paper_id = self._decide()
        self.store.submit_final(
            "alice", paper_id, "可靠分布式原子提交协议",
            "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
            ["alice", "bob"], "终稿排版。",
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式原子提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["alice", "bob"], "再次提交。",
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "final_already_submitted")
        # 新版本未生成，送审版仍是唯一版本。
        self.assertEqual(len(self.store.list_versions("alice", paper_id)["items"]), 2)

    def test_role_enforcement(self):
        paper_id = self._decide()
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "bob", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["alice", "bob"], "bob 不是论文作者。",
            )
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "r1", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["r1"], "评审人不能提交。",
            )
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.accept_final("alice", paper_id)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_final("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_invalid_authors_rejected(self):
        paper_id = self._decide()
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                [], "终稿排版。",
            )
        self.assertEqual(ctx.exception.code, "invalid_authors")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_final(
                "alice", paper_id, "可靠分布式提交协议",
                "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。",
                ["bob"], "终稿排版。",  # 缺少提交人 alice
            )
        self.assertEqual(ctx.exception.code, "invalid_authors")

    def test_diff_rules_are_order_sensitive(self):
        base = {"title": "T", "abstract": "A", "authors": ["alice", "bob"]}
        self.assertFalse(needs_chair_verification(diff_snapshots(base, base)))
        reordered = dict(base, authors=["bob", "alice"])
        self.assertTrue(needs_chair_verification(diff_snapshots(base, reordered)))
        abstract_only = dict(base, abstract="B")
        self.assertFalse(needs_chair_verification(diff_snapshots(base, abstract_only)))
        titled = dict(base, title="T2")
        self.assertTrue(needs_chair_verification(diff_snapshots(base, titled)))


if __name__ == "__main__":
    unittest.main()
