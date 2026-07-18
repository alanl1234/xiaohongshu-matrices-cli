# 跨平台支持（Platform Support）

本项目以本地优先、多账号运营为目标，支持 **Windows / macOS / Linux**。下文记录各平台的运行要点与首次配置差异。

## 已完成的跨平台对齐（代码层，无需手动改动）

| 文件 | 改动 | 原因 |
|------|------|------|
| `xhs_cli/dashboard/browser.py` | `_profile_in_use()` 增加 POSIX 分支（`pgrep` + `ps` 检测占用档案的 camoufox 进程） | 原实现在非 Windows 下直接 `return None`，会导致账号浏览器档案在 macOS / Linux 上无法打开（触发 "Unable to determine whether Camoufox is running" 安全错误）。现已与 Windows 的进程检测逻辑对等。 |
| `scripts/install_camoufox_zip.py` | 二进制名按平台区分：`camoufox.exe`（Windows）/ `camoufox`（macOS / Linux） | 原脚本硬编码 `camoufox.exe`，从非 Windows 版 ZIP 安装会失败。 |

以下机制本就跨平台，已验证无需改动：
- `_secure_profile()`：非 Windows 走 `chmod 0o700`。
- `_remove_parent_lock()`：非 Windows 直接 `unlink`。
- `cli.py` 的 `_fix_windows_encoding()`：非 Windows 提前返回。
- 数据目录使用 `Path.home() / ".xiaohongshu-cli" / "dashboard"`，跨平台。
- `.gitattributes` 已设 `eol=lf`，无 CRLF 问题。
- 无 `.bat` / `.ps1` 启动脚本，无硬编码 `C:\` 路径。

## 各平台首次运行

### 通用环境
- Python ≥ 3.10（推荐用 `uv`）。
- 安装依赖：`uv sync`（会安装 `camoufox>=0.5.3`，版本约束本身跨平台）。
- 拉取 Camoufox 二进制（按系统架构自动下载）：
  ```bash
  python -m camoufox fetch
  ```
  macOS 上 Apple Silicon 取 arm64、Intel 取 x86_64；Windows 取 win x86_64；Linux 取对应 glibc 构建。

### macOS
- 从网络下载的二进制会被 Gatekeeper 加 `com.apple.quarantine` 扩展属性，首次运行可能报「无法打开」。定位并清除隔离属性：
  ```bash
  python -c "from camoufox.multiversion import BROWSERS_DIR; print(BROWSERS_DIR)"
  xattr -cr "<上面打印出的 BROWSERS_DIR 路径>"
  # 如仍被拦截，可再允许一次
  spctl --add "<camoufox 可执行文件路径>"
  ```
  > 注：macOS 安全策略更新可能再次拦截，重跑 `xattr -cr` 即可。

### Windows
- 原生支持，无需额外配置。Camoufox 二进制为 `camoufox.exe`，浏览器档案 ACL 仅授予当前用户与 SYSTEM。
- 终端建议使用 UTF-8（项目已在 `cli.py` 内对非 UTF-8 代码页做 `reconfigure`，但用 Windows Terminal / PowerShell 7 体验更佳）。
- 从 ZIP 重装：`.venv\Scripts\python.exe scripts\install_camoufox_zip.py "C:\path\to\camoufox-<version>-win.x86_64.zip"`。

### Linux
- 与 macOS 类似，使用 `camoufox` 二进制；部分发行版需先安装 Firefox 运行所需的系统库（如 `libgtk-3-0`、`libdbus-glib-1-2` 等）。
- 浏览器档案使用 `0o700` 权限隔离。

### 启动后台（各平台通用）
```bash
export XHS_DASHBOARD_DATA="$HOME/.xiaohongshu-cli/dashboard"   # 可选，默认即此
xhs-dashboard
```
数据、cookie、登录态落在用户主目录（`~/.xiaohongshu-cli`），不进入仓库（已被 `.gitignore` 兜住）。

## 测试差异
- `tests/test_cookies.py` 的 `0o600` 权限断言为 **POSIX-only**：在 macOS / Linux 上执行（验证文件权限），在 Windows 上跳过。
- 其余测试与 Windows 版一致；需要真实 cookie / 浏览器的 `tests/test_integration.py` 在两种平台都需手动登录后跳过。

## 小结
> 跨平台适配 ≠ 改 Camoufox 版本号。真正要做的是「进程占用检测的 POSIX 实现」（已改）、「安装脚本的二进制名」（已改），以及在目标系统 `python -m camoufox fetch`（必要时在 macOS 上 `xattr -cr` 解除隔离）。
