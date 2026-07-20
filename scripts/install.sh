#!/usr/bin/env bash
#
# 一键安装脚本（macOS / Linux）
#   curl -fsSL https://raw.githubusercontent.com/alanl1234/xiaohongshu-matrices-cli/main/scripts/install.sh | bash
#
# 流程：检查 Python >= 3.11 → pip 安装 xiaohongshu-matrices-cli → 拉取 Camoufox 浏览器内核。
set -euo pipefail

PKG="xiaohongshu-matrices-cli"
MIN_MAJOR=3
MIN_MINOR=11
REPO="alanl1234/xiaohongshu-matrices-cli"

echo "==> 安装 ${PKG}"

# 1) 检查 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python3，请先安装 Python >= ${MIN_MAJOR}.${MIN_MINOR}" >&2
  exit 1
fi

# 2) 检查版本
python3 - "${MIN_MAJOR}" "${MIN_MINOR}" <<'PY'
import sys
major, minor = int(sys.argv[1]), int(sys.argv[2])
cur = sys.version_info[:2]
if cur < (major, minor):
    print(f"错误：Python {cur[0]}.{cur[1]} 过低，需要 >= {major}.{minor}", file=sys.stderr)
    sys.exit(2)
print(f"检测到 Python {cur[0]}.{cur[1]}")
PY

# 3) 安装包（--user，避免污染系统环境）
echo "==> pip install --user ${PKG}"
python3 -m pip install --user --upgrade "${PKG}"

# 4) 拉取 Camoufox 浏览器内核（抗检测 Firefox 分支）
echo "==> 拉取 Camoufox 浏览器内核"
python3 -m camoufox fetch

# 5) 提示 PATH
echo
echo "完成。请确认用户级脚本目录已在 PATH 中："
echo "  Linux/macOS: export PATH=\"\$HOME/.local/bin:\$PATH\"  （写入 ~/.bashrc / ~/.zshrc）"
echo
echo "使用方式："
echo "  xhs --help            # 命令行"
echo "  xhs-dashboard         # 启动本地后台 http://127.0.0.1:8765"
echo
echo "重新安装 / 升级："
echo "  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/scripts/install.sh | bash"
