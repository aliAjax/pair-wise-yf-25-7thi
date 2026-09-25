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

- `POST /api/papers`：提交论文。
- `GET /api/papers` / `GET /api/papers/{id}`：按角色隔离查看；评审人看到双盲视图。
- `POST /api/papers/{id}/bids`：评审意向。
- `POST /api/papers/{id}/conflicts`：主席登记利益冲突。
- `POST /api/papers/{id}/assignments`：主席邀请评审人，执行负载上限与冲突检查。
- `POST /api/assignments/{id}/respond`：接受或拒绝邀请。
- `POST /api/assignments/{id}/review`：提交 1-5 分评审。
- `POST /api/papers/{id}/rebuttal`：作者提交一次 Rebuttal。
- `POST /api/papers/{id}/decision`：收到至少两份评审后作决定。
- `POST /api/papers/{id}/final`：决定为接收或小修后，作者提交最终标题、摘要、作者顺序与排版说明；系统保留新版本并将送审版设为只读，排版说明为空则退回待提交。
- `POST /api/papers/{id}/final/approve`：主席核对定稿并签字；最终标题或作者顺序与送审版不同时必须填写变更原因，签字后论文封版。
- `GET /api/papers/{id}/final`：定稿记录（含送审版对照、变更原因与签字），仅主席与作者本人可查。
- `GET /api/papers/{id}/versions`：全部内容版本，封版后旧版仍可查；评审人视图为双盲。
- `GET /api/papers/{id}/history`：审计历史。

定稿页面入口：<http://127.0.0.1:8101/final>。

## 代码结构

- `app.py`：HTTP 层与评审主流程（`ReviewStore`）。
- `versions.py`：版本存储（`paper_versions` 表结构、迁移与读写）。
- `finalization.py`：录用定稿核对规则（提交、主席核对签字、封版）。
- `common.py`：业务错误、数据库连接与审计等共享助手。
- `web/index.html` / `web/final.html`：投稿页面与定稿页面入口。

## 业务不变量

评审人不能查看未分配论文的作者身份；利益冲突禁止投标和分配；邀请和完成状态不能跳步；每位评审人的未完成分配受 `load_limit` 限制；每篇论文只能提交一次 Rebuttal；决定必须至少基于两份已完成评审。

录用定稿：决定为接收或小修后才能提交定稿；排版说明为空则定稿退回待提交（422，不留记录）；最终标题或作者顺序与送审版不同时，主席必须填写变更原因才能签字；主席验收后论文封版，重复提交或重复签字返回 409；送审版内容、变更原因与签字记录封版后仍可查询。
