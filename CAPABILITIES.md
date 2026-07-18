# 能力总览与系统架构

`xiaohongshu-matrices-cli` 是一套**本地优先、多账号矩阵化**的小红书运营工具，提供命令行（CLI）与本地运营后台（Dashboard）两种使用方式。矩阵化的核心在于：把多个账号纳入统一纳管，按账号设定独立人设，并在同一治理引擎（限流 / 去重 / 问责）下跨账号编排发布与互动。本项目基于 [jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli)（Apache-2.0）fork，在保留原版采集与互动能力的基础上，新增了受治理的自动化编排层，可运行于 Windows / macOS / Linux。

## 能力分层

系统按职责分为五层。所有自动化行为均经过治理层把关，不会绕过任何既有闸门。

### 1. 采集层
- 按关键词 / 话题 / 作者 / 热榜 / Feed 抓取爆款笔记与评论，写入本地 SQLite 持久化存储，支持断点恢复。
- 适用场景：选题挖掘、竞品监控、爆款素材库建设。

### 2. AI 层（可选）
- 把自然语言运营目标拆解为多个检索子任务（不同角度），每个子任务自带检索词与筛选标准（`search_brief` 产出 `SearchPlan`）。
- 采集完成后，先按各角度的筛选标准做**相关性筛选**（`screen_results`，LLM 判定选中/落选并给理由，而非机械取前 N 条），再对选中素材做二创洞察分析（`material_research`）。
- 按账号人设起草草稿（`agent_draft`），默认进入 `pending_review` 状态等待人工批准。
- 走 OpenAI Responses API 兼容协议，通过 `OPENAI_API_KEY` + `OPENAI_BASE_URL` 接入任意兼容模型。

### 3. 执行层
- **浏览器发布**：`BrowserPublisher` 使用 Camoufox（Firefox 抗检测分支）以真实浏览器发布图文笔记，需 1–18 张图片。
- **互动执行**：评论、回复、私信、点赞、收藏、关注、取关。
- 任务队列：`DurableTaskQueue`（采集 / 发布）与 `OperationQueue`（AI / 互动）均为持久化队列，进程重启不丢失任务。

### 4. 治理层（核心价值与硬约束）
- 限流 / 预算上限、opt-out（退出触达）、敏感信息过滤、相似度去重、账号级问责。
- 互动灰度三段式：`shadow`（仅记录不发送）→ `inbound`（仅回复与入站私信）→ `reviewed`（全部放开）。
- 所有发布默认 `pending_review`，必须人工批准；自动化不得绕过审核。

### 5. 编排层（Orchestrator）
- 将以上四层串成 **目标 → 采集 → 研究 → 起草 → 发布 → 互动** 的闭环，完全复用既有治理引擎。
- 采用常驻轮询模型（默认 60s），目标与节奏由 `orchestrator_goals.json` 配置，全部通过环境变量 opt-in，默认关闭。
- 详见 [AI_OPERATIONS.md](./AI_OPERATIONS.md)。

## 典型场景

| 场景 | 用法 |
|------|------|
| 选题挖掘 | 目标拆成多角度检索 → 按各角度筛选标准做相关性筛选 → AI 分析「为什么火」→ 产出二创方向 |
| 内容生产 | 自然语言目标 → AI 按人设起草草稿 → 人工配图后发布 |
| 多账号矩阵 | 多账号统一纳管、按账号设定人设，治理层统一限流 / 去重 / 问责，跨账号编排发布与互动 |
| 互动运营 | `inbound` 模式自动回复评论 / 私信，受治理引擎把控 |
| 全自动闭环 | 编写目标文件，后台常驻自动跑完整条流水线 |

## 安全与合规边界
1. 自动发布必须配图：AI 仅产出文字，发布需要 1–18 张图片；未配图时草稿自动跳过、留人工处理。
2. `shadow` 模式绝不自动发送；任何触达的最终放行由 `XHS_ENGAGEMENT_MODE` 与治理引擎决定。
3. 命中敏感信息 / opt-out / 预算上限时自动转人工，不会硬闯。
4. 运行数据（Cookie / Token / 笔记库）默认落在用户主目录（`~/.xiaohongshu-cli`），不进入仓库。

## 快速开始

```bash
# 安装依赖（项目内）
uv sync
# 启动后台
xhs-dashboard --port 8765 --data-dir ./data
# 常用 CLI
xhs search "<关键词>"                  # 搜索笔记
xhs hot                                # 热榜
xhs comment <note_id> "<内容>"
xhs follow <user_id>
xhs post --title "..." --body "..." --images a.jpg b.jpg
xhs login / status / whoami            # 账号绑定与状态
```

后台地址：`http://127.0.0.1:8765`（仅本机监听）。编排器状态：`GET /api/orchestrator/status`。
