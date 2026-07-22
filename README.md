# xiaohongshu-matrices-cli

> 基于 [jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli)（Apache-2.0）的 fork，新增受治理的自动化编排层。原始版权归 `xiaohongshu-cli`。

[![CI](https://github.com/alanl1234/xiaohongshu-matrices-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alanl1234/xiaohongshu-matrices-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/badge/pypi-xiaohongshu--matrices--cli-blue.svg)](https://pypi.org/project/xiaohongshu-matrices-cli/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](https://pypi.org/project/xiaohongshu-matrices-cli/)

A local-first, multi-account matrix CLI for Xiaohongshu (小红书) — search, read, interact, and post across many accounts via a reverse-engineered API 📕

[English](#features) | [中文](#功能特性)

## Features

- 🔐 **Auth** — auto-extract browser cookies, QR code login, status check, whoami
- 🔍 **Search** — notes by keyword, user search, topic search
- 📖 **Reading** — note detail, comments, sub-comments, user profiles
- 🔢 **Short-index navigation** — open recent list results with `xhs read 1` or `xhs comments 1`
- 📰 **Feed** — recommendation feed, hot/trending by category
- 👥 **Social** — follow/unfollow, favorites
- 👍 **Interactions** — like, favorite, comment, reply, delete
- ✍️ **Creator** — post image notes, my-notes list, delete
- 🔔 **Notifications** — unread count, mentions, likes, new followers
- 🛡️ **Anti-detection** — consistent macOS Chrome fingerprint, `sec-ch-ua` alignment, session-stable browser identity, Gaussian jitter, captcha cooldown, exponential backoff
- 📊 **Structured output** — commands support `--yaml` and `--json`; non-TTY stdout defaults to YAML
- 📦 **Stable envelope** — see [SCHEMA.md](./SCHEMA.md) for `ok/schema_version/data/error`
- 🧩 **Account matrix** — manage many accounts with per-account personas; orchestrate posting and interaction across the matrix under unified rate-limiting, deduplication, and accountability

> **Note:** Non-TTY stdout (e.g. piped or called by a script/agent) defaults to YAML; pass `--json` for strict JSON.

## Installation

### 快速安装（PyPI + 一键脚本）

已发布到 PyPI，无需克隆源码即可安装（Python ≥ 3.11）：

```bash
pip install --user xiaohongshu-matrices-cli
python -m camoufox fetch          # 拉取抗检测浏览器内核
xhs-dashboard                     # 启动本地后台 http://127.0.0.1:8765
```

macOS / Linux 一键安装：

```bash
curl -fsSL https://raw.githubusercontent.com/alanl1234/xiaohongshu-matrices-cli/main/scripts/install.sh | bash
```

Windows（PowerShell）一键安装：

```powershell
irm https://raw.githubusercontent.com/alanl1234/xiaohongshu-matrices-cli/main/scripts/install.ps1 | iex
```

### 从源码安装（开发）

Clone 本仓库并用 [uv](https://github.com/astral-sh/uv) 同步依赖：

```bash
git clone https://github.com/alanl1234/xiaohongshu-matrices-cli xiaohongshu-matrices-cli
cd xiaohongshu-matrices-cli
uv sync --extra dev
```

### 界面

本工具提供 **命令行 `xhs`** 与 **本地后台 `xhs-dashboard`** 两套界面。
后台页面、账号矩阵、角色库与二创、安全模型详见 [UI.md](./UI.md)。

## Usage

```bash
# ─── Auth ─────────────────────────────────────────
xhs login                             # Extract cookies from browser
xhs login --qrcode                    # Browser-assisted QR login, scan in terminal
xhs status                            # Check login status
xhs whoami                            # Detailed profile (fans, likes, etc)
xhs whoami --json                     # Structured JSON envelope
xhs logout                            # Clear saved cookies

# ─── Search ───────────────────────────────────────
xhs search "美食"                      # Search notes
xhs search "旅行" --sort popular       # Sort: general, popular, latest
xhs search "穿搭" --type video         # Filter: all, video, image
xhs search "AI" --page 2              # Pagination
xhs search-user "用户名"               # Search users
xhs topics "美食"                      # Search hashtags/topics

# ─── Reading ──────────────────────────────────────
xhs read 1                             # Read the 1st result from the last list command
xhs read <note_id>                     # Read a note (API only)
xhs read "https://www.xiaohongshu.com/explore/xxx?xsec_token=yyy"  # Read by URL (uses URL token)
xhs comments 1                         # Read comments for the 1st result from the last list command
xhs comments "<url>"                   # View comments — paste URL to cache/reuse xsec_token
xhs comments "<url>" --all             # Fetch ALL comments (auto-paginate all pages)
xhs comments "<url>" --all --json      # All comments as JSON
xhs comments <note_id> --xsec-token T  # Use note_id + explicit xsec_token
xhs comments <note_id>                 # Reuse cached token if available
xhs sub-comments <note_id> <cmt_id>   # View replies to a comment
xhs user <user_id>                     # User profile
xhs user-posts <user_id>              # User's published notes
xhs user-posts <user_id> --cursor X   # Paginate with cursor
xhs analyze-user <user_id>            # Layered analysis of ALL posts (overview / tiers / themes / format / top / synthesis)
xhs analyze-user <user_id> --deep     # Also fetch each note's detail (collects, comments, topics, body)
xhs analyze-user <user_id> --deep --ai  # Add an AI-generated strategic summary (needs OPENAI_API_KEY)

# ─── Feed & Discovery ────────────────────────────
xhs feed                              # Recommendation feed
xhs hot                               # Hot notes (default: food)
xhs hot -c fashion                    # Categories: fashion, food, cosmetics,
                                      #   movie, career, love, home, gaming,
                                      #   travel, fitness

# Short index works after list commands such as search/feed/hot/user-posts/favorites/my-notes
xhs search "美妆"
xhs read 1
xhs comments 1
xhs like 1
xhs favorite 1

# ─── Social ───────────────────────────────────────
xhs favorites                          # My bookmarked notes (current user)
xhs favorites <user_id>                # Other user's bookmarked notes
xhs likes                             # My liked notes (current user)
xhs likes <user_id>                   # Other user's liked notes
xhs follow <user_id>                   # Follow a user
xhs unfollow <user_id>                 # Unfollow a user

# ─── Interactions ─────────────────────────────────
xhs like 1                             # Like the 1st result from the latest note listing
xhs like <note_id>                     # Like a note
xhs like <note_id> --undo             # Unlike
xhs favorite 1                         # Favorite the 1st result from the latest note listing
xhs favorite <note_id>                 # Favorite (bookmark)
xhs unfavorite 1                       # Unfavorite the 1st result from the latest note listing
xhs unfavorite <note_id>               # Unfavorite
xhs comment 1 -c "好赞！"              # Comment on the 1st result from the latest note listing
xhs comment <note_id> -c "好赞！"     # Post comment
xhs reply 1 --comment-id X -c "回复"   # Reply on the 1st result from the latest note listing
xhs reply <note_id> --comment-id X -c "回复"  # Reply to comment
xhs delete-comment <note_id> <cmt_id> # Delete own comment

# ─── Creator ─────────────────────────────────────
xhs my-notes                           # List own notes (v2 creator endpoint)
xhs my-notes --page 1                 # Next page
xhs post --title "标题" --body "正文" --images img.jpg  # Post note
# 指定话题 id 强制关联（搜索失败/无结果时避免话题变纯文字不可点击）：
xhs post --title "t" --body "b #考研" --images img.jpg --topic-id "考研=65a1b2c3..."
xhs delete <note_id>                   # Delete a PUBLISHED note (不是草稿)
xhs delete <note_id> -y               # Skip confirmation

# ─── Notifications ────────────────────────────────
xhs unread                             # Unread counts (likes, mentions, follows)
xhs notifications                      # 评论和@ notifications
xhs notifications --type likes        # 赞和收藏 notifications
xhs notifications --type connections   # 新增关注 notifications

```

> **Global `--account` option.** Every authenticated command accepts
> `--account <id|alias|xhs_user_id>` to bridge that dashboard account's cookies
> **without touching the global `cookies.json`**. This is the recommended way to
> target a specific account (e.g. `xhs --account <XHS_USER_ID> post --title …`).
> Run `xhs status` to list available dashboard accounts.
> Note: `--account` is a *global* flag, so it must come **before** the subcommand
> (`xhs --account X status`, not `xhs status --account X`).

> **📌 批量发帖一律走 CLI `--account`，不要用 Camoufox 浏览器会话导出的 cookie 去调 API。**
> 小红书的 `get_upload_permit`（上传许可）对 Camoufox 运行时会话提取的 cookie 返回**服务端错误**；
> CLI `--account` 走 `account_bridge` **离线解密**账号 profile 的 `cookies.sqlite`，已验证正常。
> Camoufox 只保留两个用途：**扫码登录**（`xhs login --qrcode`）与**纯浏览器模拟发布**（`BrowserPublisher`）。

## Authentication

xiaohongshu-matrices-cli supports multiple authentication methods:

1. **Saved cookies** — loads from `~/.xiaohongshu-cli/cookies.json`
2. **Browser cookies** — auto-detects installed browsers and extracts cookies (supports Chrome, Arc, Edge, Firefox, Safari, Brave, Chromium, Opera, Vivaldi, and more)
3. **QR code login** — browser-assisted login with terminal QR output (`xhs login --qrcode`)

`xhs login` automatically tries all installed browsers and uses the first one with valid cookies.
Use `--cookie-source <browser>` to specify a browser explicitly, or `--qrcode` for browser-assisted QR login.
Other authenticated commands automatically retry once with fresh browser cookies when the saved session has expired.

> **Browser-assisted QR login** uses the bundled [Camoufox](https://github.com/daijro/camoufox) browser. On a fresh machine you must fetch the browser binary once (per OS): `python -m camoufox fetch`. If Camoufox cannot launch (missing binary, no display, or missing system libraries — common on headless/remote machines), `xhs login --qrcode` automatically falls back to a terminal-rendered QR code. You can also force the terminal QR directly with `xhs login --qrcode --no-browser`.

### Cookie TTL

Saved cookies are valid for **7 days** by default. After that, the client automatically attempts to refresh from the browser. If browser extraction fails, the existing cookies are used with a warning.

### Multi-account: `--account`

If you run the local dashboard (`xhs-dashboard`, scanning at http://127.0.0.1:8765),
each account's cookies live in an encrypted browser profile. The CLI can bridge
any of them on demand:

```
xhs --account <XHS_USER_ID> status        # show that account's state
xhs --account <alias> read <url>        # read as that account
xhs --account <id> post --title …       # publish AS that account
```

This never writes `cookies.json`, so switching target accounts can't cross-post.
Use `xhs status` (no `--account`) to list all ready dashboard accounts.

### Draft Management

The CLI **does not manage drafts**. There is no `drafts list` / `drafts delete`
command, and the Xiaohongshu draft API endpoints return 404 — do **not** probe
them. Drafts created in the creator studio (or by the browser publisher) can only
be viewed/deleted from the creator UI at
<https://creator.xiaohongshu.com> → 草稿箱. The `xhs delete` command operates
**only on already-published notes**.

### Short-Index Navigation

After any listing command such as `search`, `feed`, `hot`, `user-posts`, `favorites`, or `my-notes`, the CLI stores the latest ordered note list in `~/.xiaohongshu-cli/index_cache.json`.

- `xhs read <N>` opens the Nth note from the latest listing
- `xhs comments <N>` opens comments for the Nth note from the latest listing
- `xhs like <N>`, `xhs favorite <N>`, `xhs unfavorite <N>`, `xhs comment <N>`, and `xhs reply <N>` reuse the same short index
- Empty listings clear the index cache, so old results are not reused by accident

## Environment Variables

### CLI

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT` | `auto` | Output format: `json`, `yaml`, `rich`, or `auto` (→ YAML when non-TTY) |

### Dashboard (`xhs-dashboard`)

| Variable | Default | Description |
|----------|---------|-------------|
| `XHS_DASHBOARD_DATA` | `~/.xiaohongshu-cli/dashboard` | Dashboard data directory (SQLite, uploads, profiles) |
| `XHS_WORKERS` | `2` | Background worker threads |
| `XHS_REQUEST_INTERVAL` | `1.0` | Min seconds between XHS API requests |
| `XHS_DAILY_REQUEST_LIMIT` | `2500` | Max requests per account per day |
| `XHS_PUBLISH_COOLDOWN` | `600` | Seconds between publishes per account |
| `XHS_QUEUE_LEASE` | `180` | Task lease seconds (resilience to crashes) |
| `XHS_QUEUE_POLL` | `0.5` | Queue poll interval seconds |
| `XHS_AGENT_TOKEN` | _(empty)_ | Token guarding agent-inbox API endpoints |

### Orchestrator (auto pipeline — all opt-in, off by default)

| Variable | Default | Description |
|----------|---------|-------------|
| `XHS_ORCHESTRATOR` | _(off)_ | Set `1` to start the orchestrator loop |
| `XHS_ORCHESTRATOR_TICK` | `60` | Orchestrator poll interval (seconds, min 10) |
| `XHS_AUTO_PUBLISH` | _(off)_ | `1` = convert gate-complete drafts to pending review; legacy `approve` behaves the same and never bypasses human approval |
| `XHS_ASSET_POOL_DIR` | _(empty)_ | Legacy compatibility setting; gate-complete drafts must name their exact final images |
| `XHS_DAILY_PUBLISH_LIMIT` | `5` | Max auto-publishes per account per day |
| `XHS_ENGAGEMENT_MODE` | `shadow` | `shadow` / `inbound` / `reviewed` — grayscale for auto engagement |
| `XHS_AUTO_ENGAGE` | _(off)_ | Set `1` to auto-execute approved engagement tasks |

### AI (optional — for research/drafting)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | _(empty)_ | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL (self-hosted/gateway) |
| `XHS_AI_FAST_MODEL` | `gpt-4o-mini` | Fast model for search briefs |
| `XHS_AI_BALANCED_MODEL` | `gpt-4o` | Balanced model for research/drafting |
| `XHS_AI_QUALITY_MODEL` | = balanced | Quality model |

> See [`AI_OPERATIONS.md`](./AI_OPERATIONS.md) for the full orchestration & governance guide.

## Rate Limiting & Anti-Detection

xiaohongshu-matrices-cli includes comprehensive anti-risk-control measures designed to minimize detection:

### Request Timing
- **Gaussian jitter**: Delays between requests use a truncated Gaussian distribution (not fixed intervals) to mimic natural browsing patterns
- **Random long pauses**: ~5% of requests include an additional 2-5 second delay simulating reading behavior
- **Auto-retry**: Exponential backoff on HTTP 429/5xx and network errors (up to 3 retries)

### Browser Fingerprint Consistency
- **UA/Platform alignment**: User-Agent, `sec-ch-ua`, `sec-ch-ua-platform`, and fingerprint fields are all consistent (macOS Chrome 145)
- **Session-stable identity**: GPU, screen resolution, CPU cores, and other hardware fingerprint values are generated once per session and reused across all requests (real browsers don't change hardware mid-session)
- **macOS-native values**: GPU vendors (Apple M1/M2/M3, Intel Iris), Retina screen resolutions, `MacIntel` platform — all matching a real macOS browser
- **Host-OS independent**: this fingerprint is a deliberately fixed anti-detection identity (macOS Chrome). It is used identically on Windows / macOS / Linux and does not require the host to be macOS.

### Captcha Cooldown
- **Progressive backoff**: On captcha trigger (HTTP 461/471), automatically sleeps 5→10→20→30 seconds with increasing delays
- **Adaptive rate limiting**: Request delay is permanently doubled after a captcha event to reduce future risk

### Signed Requests
- All API calls use `x-s` / `x-s-common` / `x-t` signatures (reverse-engineered from web client)
- `x-b3-traceid` and `x-xray-traceid` for distributed tracing consistency

### Layered account analysis (`analyze-user`)

Give a Xiaohongshu **user id** (the public author id, e.g. `95653634553` — not a local
dashboard account) and the tool pulls **all** of that account's posts and prints a
hierarchical report:

- **L0 账号总览** — total posts, cumulative/average/median/max likes, media mix, date span
- **L1 互动分层** — engagement tiers (爆款 ≥1万 / 优质 ≥1千 / 普通 ≥100 / 潜力)
- **L2 主题聚类** — clusters by topic tags (with `--deep`) or title keyword frequency
- **L3 形式与节奏** — video vs image performance + best posting weekday/window
- **L4 头部帖子** — top-N posts by engagement
- **L5 战略总结** — prose synthesis (rule-based, or AI with `--ai` + `OPENAI_API_KEY`)

By default only the `user_posted` list API is used (likes + form + themes + cadence).
Add `--deep` to also fetch each note's detail for collects/comments/topics/body — slower
but more accurate for L1–L3. The command uses the same `--account` cookie bridge as the
publisher, so no global `cookies.json` mutation and no browser launch. Output with
`--json`/`--yaml` for agent consumption (matches the [SCHEMA.md](./SCHEMA.md) envelope).

## Structured Output

All `--json` / `--yaml` output uses the shared envelope from [SCHEMA.md](./SCHEMA.md):
```yaml
ok: true
schema_version: "1"
data: { ... }
```

When stdout is not a TTY (e.g., piped or invoked by an AI agent), output defaults to YAML.
Use `OUTPUT=yaml|json|rich|auto` to override.

## Use as AI Agent Skill

xiaohongshu-matrices-cli ships with a [`SKILL.md`](./SKILL.md) that teaches AI agents how to use it.

### [Skills CLI](https://github.com/vercel-labs/skills) (Recommended)

```bash
npx skills add jackwener/xiaohongshu-cli
```

| Flag | Description |
| --- | --- |
| `-g` | Install globally (user-level, shared across projects) |
| `-a claude-code` | Target a specific agent |
| `-y` | Non-interactive mode |

### Manual Install

```bash
mkdir -p .agents/skills
git clone git@github.com:jackwener/xiaohongshu-cli.git .agents/skills/xiaohongshu-cli
```

### ~~OpenClaw / ClawHub~~ (Deprecated)

> ⚠️ ClawHub install method is deprecated and no longer supported. Use [Skills CLI](#skills-cli-recommended) or Manual Install above.

## Project Structure

```text
xhs_cli/
├── __init__.py
├── cli.py              # Click entry point & command registration
├── client.py           # XHS API client (signing, retry, rate-limit, anti-detection)
├── cookies.py          # Cookie extraction, TTL management, auto-refresh, token cache
├── signing.py          # Main API x-s / x-s-common signature generation
├── creator_signing.py  # Creator API AES-128-CBC signature
├── constants.py        # URLs, User-Agent, Chrome version, SDK config
├── exceptions.py       # Structured exception hierarchy (6 error types)
├── qr_login.py         # QR code login (browser-assisted terminal QR + HTTP fallback)
├── formatter.py        # Output formatting, schema envelope, Rich rendering
└── commands/
    ├── _common.py      # Shared CLI helpers (structured_output_options, etc.)
    ├── auth.py         # login/logout/status/whoami
    ├── reading.py      # search/read/comments/user/feed/hot/topics/search-user
    ├── interactions.py  # like/favorite/comment/reply/delete-comment
    ├── social.py       # follow/unfollow/favorites
    ├── creator.py      # post/my-notes/delete
    └── notifications.py # unread/notifications
```

## Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Unit tests only (no network)
uv run pytest tests/ -v --ignore=tests/test_integration.py -m "not smoke"

# Smoke tests (need cookies)
uv run pytest tests/ -v -m smoke

# Integration tests (need cookies)
uv run pytest tests/test_integration.py -v

# Lint
uv run ruff check .
```

## Troubleshooting

**Q: `NoCookieError: No 'a1' cookie found`**

1. Open any browser and visit https://www.xiaohongshu.com/
2. Log in with your account
3. Run `xhs login` (auto-detects browser) or `xhs login --cookie-source <browser>`

**Q: `NeedVerifyError: Captcha required`**

XHS has triggered a captcha check. Open https://www.xiaohongshu.com/ in your browser, complete the captcha, then retry.

**Q: `IpBlockedError: IP blocked by XHS`**

Try a different network (e.g., mobile hotspot or VPN). XHS blocks IPs that make too many requests.

**Q: `SessionExpiredError: Session expired`**

Your cookies have expired. Run `xhs login` to refresh.

**Q: Requests are slow**

The built-in Gaussian jitter delay (~1-1.5s between requests) is intentional to mimic natural browsing and avoid triggering XHS's risk control. Aggressive request patterns may lead to captcha triggers or IP blocks.



## 功能特性

- 🔐 **认证** — 自动提取浏览器 Cookie，browser-assisted 二维码扫码登录，状态检查，用户信息
- 🔍 **搜索** — 按关键词搜索笔记、用户、话题
- 📖 **阅读** — 笔记详情、评论、子评论、用户主页
- 📰 **发现** — 推荐 Feed、按分类浏览热门
- 👥 **社交** — 关注/取关、收藏夹
- 👍 **互动** — 点赞、收藏、评论、回复、删除
- ✍️ **创作者** — 发布图文笔记、我的笔记列表、删除
- 🔔 **通知** — 未读数、@、点赞、新关注
- 🛡️ **反风控** — macOS Chrome 指纹一致性、session 级浏览器身份持久化、高斯抖动延迟、验证码自动冷却、指数退避重试
- 📊 **结构化输出** — `--yaml` / `--json`，非 TTY 默认输出 YAML
- 📦 **稳定 envelope** — 参见 [SCHEMA.md](./SCHEMA.md)
- 🧩 **账号矩阵** — 多账号统一管理，按账号设定人设；在统一的限流 / 去重 / 问责治理下跨账号编排发布与互动

## 安装

本项目以源码方式分发。克隆本仓库并用 [uv](https://github.com/astral-sh/uv) 同步依赖：

```bash
git clone <你的 GitHub 仓库地址> xiaohongshu-matrices-cli
cd xiaohongshu-matrices-cli
uv sync
```

上游基础项目 `xiaohongshu-cli` 仍发布在 PyPI，如需上游包可自行安装。更新时拉取最新代码后重新执行 `uv sync` 即可。

## 使用示例

```bash
# 认证
xhs login                             # 从浏览器提取 Cookie
xhs login --qrcode                    # browser-assisted 二维码扫码登录（终端显示二维码）
xhs status                            # 检查登录状态
xhs whoami                            # 查看用户资料
xhs logout                            # 清除缓存的 Cookie

# 搜索
xhs search "美食"                      # 搜索笔记
xhs search "旅行" --sort popular       # 排序：general, popular, latest
xhs search-user "用户名"               # 搜索用户
xhs topics "美食"                      # 搜索话题

# 阅读
xhs read 1                             # 阅读最近一次列表里的第 1 条笔记
xhs read <note_id>                     # 阅读笔记（仅走 API）
xhs read "https://...?xsec_token=..."  # 粘贴网页 URL 直接阅读（使用 URL token）
xhs comments 1                         # 查看最近一次列表里的第 1 条笔记评论
xhs comments "<url>"                   # 查看评论 — 粘贴 URL 以缓存/复用 xsec_token
xhs comments "<url>" --all             # 获取全部评论（自动翻页）
xhs comments "<url>" --all --json      # 全部评论，JSON 格式
xhs comments <note_id> --xsec-token T  # 用 note_id + 显式 xsec_token
xhs comments <note_id>                 # 如果之前访问过 URL，会复用缓存 token
xhs sub-comments <note_id> <cmt_id>   # 查看评论的回复
xhs user <user_id>                     # 用户主页
xhs user-posts <user_id>              # 用户发布的笔记
xhs analyze-user <user_id>            # 分层式分析某账号全部帖子（总览/分层/主题/形式/头部/总结）
xhs analyze-user <user_id> --deep      # 逐篇补详情（收藏/评论/话题/正文），L1–L3 更准
xhs analyze-user <user_id> --deep --ai # 加 AI 战略总结（需 OPENAI_API_KEY）

# 发现
xhs feed                              # 推荐 Feed
xhs hot -c food                       # 热门笔记（按分类）
xhs hot -c travel                     # 分类: fashion, food, cosmetics, movie, career,
                                      #       love, home, gaming, travel, fitness

# 社交
xhs favorites                          # 我的收藏（自动识别当前用户）
xhs favorites <user_id>                # 其他用户的收藏
xhs likes                            # 我的点赞（自动识别当前用户）
xhs likes <user_id>                  # 其他用户的点赞
xhs follow <user_id>                   # 关注
xhs unfollow <user_id>                 # 取消关注

# 互动
xhs like 1                             # 给最近一次列表里的第 1 条笔记点赞
xhs like <note_id>                     # 点赞
xhs like <note_id> --undo              # 取消点赞
xhs favorite 1                         # 收藏最近一次列表里的第 1 条笔记
xhs favorite <note_id>                 # 收藏
xhs unfavorite 1                       # 取消收藏最近一次列表里的第 1 条笔记
xhs unfavorite <note_id>               # 取消收藏
xhs comment 1 -c "好棒！"              # 给最近一次列表里的第 1 条笔记发评论
xhs comment <note_id> -c "好棒！"      # 发评论
xhs reply 1 --comment-id X -c "谢谢"   # 给最近一次列表里的第 1 条笔记回复评论
xhs reply <note_id> --comment-id X -c "谢谢"  # 回复评论
xhs delete-comment <note_id> <cmt_id>  # 删除自己的评论

# 创作者
xhs my-notes                           # 我的笔记列表
xhs post --title "标题" --body "正文" --images img.jpg  # 发布笔记
xhs delete <note_id>                   # 删除笔记
xhs delete <note_id> -y                # 跳过确认

# 通知
xhs unread                             # 未读数
xhs notifications                      # 评论和 @ 通知
xhs notifications --type likes         # 赞和收藏通知
xhs notifications --type connections   # 新增关注通知
```

## 认证策略

xiaohongshu-matrices-cli 支持多种认证方式：

1. **已保存 Cookie** — 从 `~/.xiaohongshu-cli/cookies.json` 加载
2. **浏览器 Cookie** — 自动检测已安装浏览器并提取（支持 Chrome、Arc、Edge、Firefox、Safari、Brave、Chromium、Opera、Vivaldi 等）
3. **二维码扫码登录** — browser-assisted 登录，终端显示二维码，用小红书 App 扫码（`xhs login --qrcode`）

Cookie 保存后有效期 **7 天**，超时后自动尝试从浏览器刷新。

`xhs login` 会自动尝试所有已安装浏览器，使用第一个有有效 Cookie 的浏览器。也可用 `--cookie-source <browser>` 指定浏览器，或 `--qrcode` 使用 browser-assisted 二维码登录。其他需认证命令在 session 过期时会自动重试一次。

> **browser-assisted 二维码登录**使用内置的 [Camoufox](https://github.com/daijro/camoufox) 浏览器。新机器需先下载浏览器二进制（每个系统一次）：`python -m camoufox fetch`。若 Camoufox 无法启动（二进制缺失、无显示环境、或缺系统库——在无显示/远程机器上很常见），`xhs login --qrcode` 会自动回退到终端二维码。也可以直接用 `xhs login --qrcode --no-browser` 强制走终端二维码。

## 常见问题

- `NoCookieError: No 'a1' cookie found` — 请先在任意浏览器打开 https://www.xiaohongshu.com/ 并登录，然后执行 `xhs login`
- `NeedVerifyError` — 触发了验证码，请到浏览器中完成验证后重试
- `IpBlockedError` — IP 被限制，尝试切换网络（手机热点或 VPN）
- `SessionExpiredError` — Cookie 过期，执行 `xhs login` 刷新
- 请求较慢是正常的 — 内置高斯随机延迟（~1-1.5s）是为了模拟人类浏览行为，避免触发风控

## 作为 AI Agent Skill 使用

xiaohongshu-matrices-cli 自带 [`SKILL.md`](./SKILL.md)，让 AI Agent 能自动学习并使用本工具。

### [Skills CLI](https://github.com/vercel-labs/skills)（推荐）

```bash
npx skills add jackwener/xiaohongshu-cli
```

| 参数 | 说明 |
| --- | --- |
| `-g` | 全局安装（用户级别，跨项目共享） |
| `-a claude-code` | 指定目标 Agent |
| `-y` | 非交互模式 |

### 手动安装

```bash
mkdir -p .agents/skills
git clone git@github.com:jackwener/xiaohongshu-cli.git .agents/skills/xiaohongshu-cli
```

### ~~OpenClaw / ClawHub~~（已过时）

> ⚠️ ClawHub 安装方式已过时，不再支持。请使用上方的 Skills CLI 或手动安装。

## 关于本仓库（Fork 说明）

本仓库基于 [jackwener/xiaohongshu-cli](https://github.com/jackwener/xiaohongshu-cli)（Apache-2.0）fork，并新增了一层**受治理的全自动编排**：

- 本地多账号运营后台 `xhs-dashboard`（`xhs_cli/dashboard/`）
- 编排调度模块 `xhs_cli/dashboard/orchestrator.py`：目标 → 多角度检索（LLM 拆解子任务）→ 相关性筛选（非前 N）→ AI 素材研究 → AI 起草 → 发布/互动 的闭环，复用项目既有的治理引擎（限流 / opt-out / 敏感词 / 相似度），所有自动化行为均走环境变量 opt-in。
- 相关文档：`AI_OPERATIONS.md`、`CAPABILITIES.md`、`DASHBOARD.md`、`INTERACTION_RULES.md`、`PLATFORMS.md`、`orchestrator_goals.example.json`

本仓库已做跨平台对齐，可在 Windows / macOS / Linux 上运行，详见 [PLATFORMS.md](./PLATFORMS.md)。

第三方依赖 `camoufox` 以源码形式 vendored 在 `vendor/camoufox-python/`，通过 `pyproject.toml` 的 `[tool.uv.sources]` 作为本地 uv 源引入（MIT License, Copyright daijro）。运行数据（cookie / token / 笔记库）默认落在用户主目录（`~/.xiaohongshu-cli`），不会进入仓库。

## 免责声明

本项目为技术研究与学习工具。自动化操作可能违反小红书用户协议，使用者应自行承担账号风控、限流、封禁等一切后果。作者不对因使用本项目导致的任何账号损失或其他损害承担责任。

## License

Apache-2.0
