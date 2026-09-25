"""定稿核对规则：比较送审版快照与定稿快照。

规则独立于存储（versioning.py）和页面/HTTP 入口（app.py、web/final.html）维护：
只有最终标题或作者顺序发生变化时，才需要主席核对并填写变更原因；
摘要与排版说明的变化只在版本中留痕，不强制核对。
"""
from __future__ import annotations

from typing import Any, Mapping

# 需要主席核对的变更字段：标题、作者顺序（顺序敏感，成员变化也算）。
CHAIR_FIELDS = ("title_changed", "authors_changed")


def diff_snapshots(submission: Mapping[str, Any], camera_ready: Mapping[str, Any]) -> dict[str, bool]:
    """返回送审版到定稿的变更标记。作者顺序按有序列表逐位比较。"""
    old_authors = list(submission.get("authors") or [])
    new_authors = list(camera_ready.get("authors") or [])
    return {
        "title_changed": (submission.get("title") or "").strip() != (camera_ready.get("title") or "").strip(),
        "abstract_changed": (submission.get("abstract") or "").strip() != (camera_ready.get("abstract") or "").strip(),
        "authors_changed": old_authors != new_authors,
    }


def needs_chair_verification(changes: Mapping[str, bool]) -> bool:
    """标题或作者顺序不同，必须经主席核对。"""
    return any(bool(changes.get(field)) for field in CHAIR_FIELDS)


def change_reason_required(changes: Mapping[str, bool], reason: str) -> bool:
    """存在强制核对项且原因为空时，需要主席补充原因。"""
    return needs_chair_verification(changes) and not (reason or "").strip()
