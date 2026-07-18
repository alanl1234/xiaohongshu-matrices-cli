# 小红书爆款采集与多账号发布后台

## 启动

```bash
uv sync --extra dev
# macOS / Linux:  .venv/bin/xhs-dashboard
# Windows:        .venv\Scripts\xhs-dashboard
xhs-dashboard            # 启动后台（uv sync 后可用）
```

## Camoufox 浏览器（跨平台）

项目通过本地 `vendor/camoufox-python` 接口使用 Camoufox（Firefox 抗检测分支）。
Camoufox 二进制按操作系统下载，使用对应平台的安装命令即可，无需修改版本号：

```bash
# 拉取当前系统的 Camoufox 二进制（自动识别架构）
python -m camoufox fetch

# 或：从已下载的 ZIP 重新安装（路径按实际位置填写）
#   macOS / Linux:  python scripts/install_camoufox_zip.py "/path/to/camoufox-<version>-<os>.zip"
#   Windows:        .venv\Scripts\python.exe scripts\install_camoufox_zip.py "C:\path\to\camoufox-<version>-win.x86_64.zip"
```

安装器会根据同步索引校验 SHA-256，并在确认二进制存在后才切换活动版本。
打开 `http://127.0.0.1:8765`。后台只允许监听本机地址，数据默认保存在
用户主目录下的 `.xiaohongshu-cli/dashboard`（Windows 为 `%USERPROFILE%\.xiaohongshu-cli\dashboard`）。
可用 `--data-dir` 修改位置。

## P0 可靠性与安全

- 搜索和发布任务写入 SQLite 持久化队列，使用租约与心跳；服务重启后搜索可断点恢复。
- 同一账号严格串行，不同账号默认最多两个低并发工作线程。
- 发布线程在提交阶段中断时直接转为 `verification_pending`，不会自动再次点击发布。
- 搜索候选逐篇落库，已完成候选不会在恢复后重复抓取。
- 每账号默认读取间隔 1 秒、每日最多 2500 次读取请求；验证、限流或账号限制会暂停账号。
- 发布前校验标题、正文加话题总长度、1–18 张图片、单图 20MB 和重复内容指纹。
- 发布核验要求本次提交后出现新的笔记 ID，不再只按相同标题判断。
- 浏览器档案按平台隔离：Windows 下档案 ACL 仅授予当前用户与 SYSTEM；其他系统使用 `0o700` 权限，并拒绝同一小红书身份绑定到两个档案。
- 数据库首次升级到 P0 架构时，会在数据目录的 `backups` 中自动保存迁移前备份。

可通过环境变量调整：`XHS_WORKERS`、`XHS_REQUEST_INTERVAL`、
`XHS_DAILY_REQUEST_LIMIT`、`XHS_QUEUE_LEASE` 和 `XHS_PUBLISH_COOLDOWN`。

## 首次使用

1. 在“账号管理”创建档案，点击“扫码绑定”，在弹出的独立浏览器中登录。
2. 绑定状态变为 `ready` 后，在“爆款搜索”创建采集任务。
3. 任务命中的笔记会连同图片和最多 100 条评论写入本地素材库。
4. 在“审核与发布”上传内容，或导入批量目录；批准后才能执行发布。

旧版单账号 Cookie 可一次性迁移：

```powershell
python -m xhs_cli.dashboard.migrate
```

## 批量发布目录

每篇内容一个文件夹：

```text
待发布素材/
  帖子一/
    post.md
    01.jpg
    02.jpg
```

`post.md` 示例：

```markdown
---
title: 周末城市漫步
account: 品牌主账号
topics: [城市漫步, 周末去哪]
images: [01.jpg, 02.jpg]
---
正文内容。
```

## 安全与风控

- 浏览器会话按账号隔离，数据库不保存明文 Cookie。
- 所有发布任务默认是 `pending_review`，必须人工批准。
- 同账号发布串行，默认间隔至少 10 分钟。
- 验证码、登录失效、限流和访问限制会暂停任务，不进行自动绕过。
- `verification_pending` 表示页面可能提示成功但作品列表未确认，系统不会自动重发。
- 首次真实发布建议使用测试账号或仅自己可见内容。
