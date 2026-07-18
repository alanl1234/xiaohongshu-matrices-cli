# AI 素材研究与互动运营扩展

面向多账号矩阵运营：在统一治理下，把自然语言目标拆解为检索子任务，并跨账号编排发布与互动。

## 启动

在项目根目录下设置环境变量后启动后台。Windows 使用 `$env:VAR="值"`，macOS / Linux 使用 `export VAR=值`。

```bash
# 方式一：依赖同步完成后（uv sync 之后）
xhs-dashboard

# 方式二：使用项目虚拟环境
#   macOS / Linux:  .venv/bin/xhs-dashboard
#   Windows:        .venv\Scripts\xhs-dashboard
```

```bash
export OPENAI_API_KEY="你的 API Key"
export XHS_AGENT_TOKEN="自行生成的长随机字符串"
export XHS_ENGAGEMENT_MODE="shadow"   # shadow / inbound / reviewed
xhs-dashboard
```

后台默认地址为 `http://127.0.0.1:8765`，仅监听本机。Camoufox 不需要一直打开；绑定、同步或执行任务时会按账号打开，完成后关闭，后台进程需保持运行。

模型均可通过环境变量替换：

默认 `shadow` 只生成和审核，不执行发送；第二阶段改为 `inbound`，只开放自有评论回复和入站私信；完成专用测试账号验收后才能人工改为 `reviewed`。

```bash
export XHS_AI_FAST_MODEL="低成本分类模型"
export XHS_AI_BALANCED_MODEL="日常研究和起草模型"
export XHS_AI_QUALITY_MODEL="复杂研究升级模型"
```

API Key 和 Agent Token 不写入 SQLite、任务包或日志。

## 推荐使用顺序

1. 在“账号人设”中建立真实账号的人设版本。
2. 在“AI 研究”中把自然语言目标拆成多个检索角度（每个角度带检索词与筛选标准），完成后每个角度生成一个采集任务。
3. 在“授权素材与二创”中登记来源。只有 `owned` 或 `authorized` 可以进入二创。
4. 在线下完成二创，成品目录放置 `post.md` 和本地图片，再人工声明使用权并导入。
5. 在“互动工作台”登记不含消息正文的会话索引，创建评论或私信任务。
6. 每条任务先批准，再单独点击执行。批准与执行是两个不同动作。

## Agent Gateway

OpenAPI 文档位于 `http://127.0.0.1:8765/api/docs`。所有 `/api/agent/*` 请求必须携带：

```text
X-Agent-Token: <XHS_AGENT_TOKEN>
```

外部 Agent 可以创建 AI 任务、草稿和待审核互动任务，但没有批准、发送、Cookie、浏览器档案或敏感会话读取接口。

创建草稿示例：

```json
{
  "kind": "comment",
  "account_id": 1,
  "content": "针对当前笔记内容的具体观点",
  "sources": ["note:abc"]
}
```

创建互动任务示例：

```json
{
  "kind": "dm_reply",
  "account_id": 1,
  "thread_id": 3,
  "content": "可以，我们继续在站内把需求确认清楚。"
}
```

Agent 也可将 UTF-8 JSON 文件放入数据目录的 `agent-inbox`，由操作者在“AI 研究”页手动导入。成功导入后扩展名变为 `.imported`。

## 隐私和停止规则

- 系统在内容进入模型和数据库前检测手机号、微信号及地址。
- 同步私信时一旦检测到敏感信息，只写入不含原文的事件，停止 AI 和自动发送，并要求到原账号查看。
- 拒绝、投诉或举报会把目标加入跨账号停止触达名单。
- 验证码、登录失效、账号限制和发送结果不明确会停止队列，不自动重发。
- 详细规则参见 `INTERACTION_RULES.md`，后台“互动规则”页展示当前生效版本。

## 外部 AI Agent / 自动化工具

优先使用 OpenAPI 连接器；如果工具只能访问本地文件，则只授权 `agent-inbox` 目录。不要把后台数据根目录、浏览器档案目录或账号 Cookie 暴露给外部 Agent。

## Agent 发布门禁

Agent 通过 `PUT /api/agent/drafts/{draft_id}/publish-gate` 填充门禁：素材权属（自有或已授权）、来源引用、二创完成声明、1–18 个成品图片绝对路径、权属证据，以及可选话题。编排器仅使用这些明确登记的成品图片，并且只能生成 `pending_review` 任务，不能批准。

## 全自动编排（Orchestrator）

编排层在**不改动任何手动流程**的前提下，把已有的两套队列（采集/发布、AI/互动）串成闭环，
并一律经过治理引擎（限流、opt-out、敏感词、相似度）把关。默认关闭，需显式开启。

### 环境变量

| 变量 | 取值 | 作用 |
|------|------|------|
| `XHS_ORCHESTRATOR` | `1` | 启动编排常驻线程：目标调度 + 流水线串联 + 自动发布 + 自动执行已批准互动 |
| `XHS_AUTO_PUBLISH` | `1` / `approve` | `1`=门禁完整的 AI 草稿转待审核发布任务；旧值 `approve` 行为相同，始终需要人工批准 |
| `XHS_ASSET_POOL_DIR` | 目录路径 | 旧版兼容变量；新流程只使用发布门禁明确填入的成品图片 |
| `XHS_ENGAGEMENT_MODE` | `shadow`/`inbound`/`reviewed` | 复用既有互动灰度，决定哪些**已批准**任务被自动执行 |
| `XHS_AUTO_ENGAGE` | `1` | 额外开启入站会话的定期同步（dm_sync） |
| `XHS_DAILY_PUBLISH_LIMIT` | 整数（默认 5） | 每账号每日最多自动发布数 |
| `XHS_ORCHESTRATOR_TICK` | 秒（默认 60） | 编排线程轮询间隔 |

### 目标文件

在数据目录 `orchestrator_goals.json` 放置运营目标列表（可参考 `orchestrator_goals.example.json`）。
编排器按各自 `cadence_hours` 周期性把目标转成 `search_brief` AI 任务，之后自动串联：

```
目标(orchestrator_goals.json)
  → search_brief（AI 把目标拆成多个检索子任务，各带检索词与筛选标准，产出 SearchPlan）
  → 每个子任务一个采集任务（自动入库爆款笔记）
  → screen_results（AI 按各角度筛选标准做相关性筛选，选中/落选给理由，非热度前 N）
  → material_research（AI 分析选中素材、产出二创洞察）
  → agent_draft（AI 按人设起草草稿，状态 pending_review）
  → Agent 填充来源、授权、二创完成和成品图片门禁
  → [XHS_AUTO_PUBLISH] 门禁完整后转为 pending_review（人工批准）
```

### 互动侧的"全自动"

互动任务仍需先 `pending_review → approved`（Agent Gateway 只能创建，批准必须由人工在后台完成）。
编排器在 `reviewed`/`inbound` 模式下，把**已批准**任务自动排队执行，并再次经 `GovernanceService.preflight`
把关（预算、冷却、opt-out、敏感词）。`shadow` 模式下不自动执行任何发送。

### 状态与手动触发

- `GET /api/orchestrator/status`：查看是否启用、当前模式、已加载目标数、是否运行中。
- `POST /api/orchestrator/trigger`：立即跑一轮编排（即使常驻线程未启用也会执行一次）。

> 设计原则：编排器只做"串联与排期"，不绕过任何既有闸门。发布与主动触达的最终放行，
> 始终由治理引擎与 `XHS_ENGAGEMENT_MODE` 决定；命中敏感信息 / opt-out / 预算上限会自动转人工。
