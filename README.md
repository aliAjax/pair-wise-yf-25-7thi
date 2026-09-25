# 学术会议同行评审系统

一个仅使用 Python 3.11+ 标准库的独立示例项目。SQLite 保存数据，`http.server` 提供 JSON API 和演示页面。

## 运行

```bash
python app.py --init --seed
python app.py
```

访问 <http://127.0.0.1:8101>。默认数据库为 `review.db`，端口为 `8101`。测试：

```bash
python -m unittest -v
```

## 角色和主要接口

演示用户：`alice`、`bob`（作者），`r1`、`r2`、`r3`（评审人），`chair`（主席）。所有 API 请求应带 `X-User-Id` 请求头。

- `POST /api/papers`：提交论文（可选 `authors` 数组指定作者顺序，默认仅提交人）。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `GET /api/papers/{id}/history`：审计历史。

### 录用定稿（页面入口 <http://127.0.0.1:8101/final>）

决定为**接收（accept）或小修（minor_revision）**后进入定稿流程：

- `GET /api/papers/{id}/final`：查定稿状态（作者本人或主席）。
- `POST /api/papers/{id}/final`：作者提交最终标题、摘要、作者顺序（`authors`）和排版说明（`typesetting`）。系统保留为新版本（`paper_versions.kind='camera_ready'`），并立即把送审版锁定为只读（`locked=1`），旧版本不会被覆盖。
  - 排版说明为空：返回 `422 typesetting_required`，退回"待提交"，不产生新版本。
  - 已在核对/验收中重复提交：`409 final_already_submitted`；封版后重复提交：`409 paper_finalized`。
  - 拒稿/大修论文：`409 final_not_eligible`。
- `POST /api/papers/{id}/final/verify`：仅主席。最终标题或作者顺序与送审版不同时，必须核对并填写变更原因（空原因返回 `422 change_reason_required`），核对后进入待验收。
- `POST /api/papers/{id}/final/accept`：仅主席。无变更可直接验收；有变更必须先核对（`409 verification_required`）。验收后论文封版（`papers.status='finalized'`）。
- `GET /api/papers/{id}/versions`：查全部版本。送审版（只读）、定稿、变更原因、核对/验收签字记录（`verified_by/at`、`accepted_by/at`）封版后仍可查。

定稿功能按三个关注点分开维护：

| 关注点 | 位置 |
| --- | --- |
| 版本存储（快照、送审版只读锁、定稿记录、旧库迁移） | `versioning.py`（`VersionStore`） |
| 核对规则（标题/作者顺序差异、是否需要主席核对） | `verification.py`（纯函数） |
| 页面入口（作者提交、主席核对验收） | `web/final.html`（路由 `/final`） |

状态机：`pending_submission → pending_verification（标题或作者顺序有变）/ pending_acceptance（无强制核对项）→ finalized`。作者顺序按有序列表比较；仅摘要变化不要求主席核对。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

录用定稿不变量：只有接收/小修论文可提交定稿；排版说明为空退回待提交且不留版本；定稿提交后送审版立即只读、旧版本永不被覆盖；最终标题或作者顺序与送审版不同时必须经主席核对并填写变更原因才能验收；验收封版后任何重复提交/验收返回 409，旧版本、变更原因和签字记录仍可查询。
