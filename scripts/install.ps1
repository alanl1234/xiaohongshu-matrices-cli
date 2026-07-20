# 一键安装脚本（Windows PowerShell）
#   irm https://raw.githubusercontent.com/alanl1234/xiaohongshu-matrices-cli/main/scripts/install.ps1 | iex
#
# 流程：检查 Python >= 3.11 → pip 安装 xiaohongshu-matrices-cli → 拉取 Camoufox 浏览器内核。
$ErrorActionPreference = "Stop"

$PKG = "xiaohongshu-matrices-cli"
$MIN_MAJOR = 3
$MIN_MINOR = 11

Write-Host "==> 安装 $PKG"

# 1) 检查 python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "错误：未找到 python，请先安装 Python >= $MIN_MAJOR.$MIN_MINOR" -ForegroundColor Red
    exit 1
}

# 2) 检查版本
$ver = python -c "import sys; print('%d.%d' % sys.version_info[:2])"
$parts = $ver.Split('.')
if ([int]$parts[0] -lt $MIN_MAJOR -or ([int]$parts[0] -eq $MIN_MAJOR -and [int]$parts[1] -lt $MIN_MINOR)) {
    Write-Host "错误：Python $ver 过低，需要 >= $MIN_MAJOR.$MIN_MINOR" -ForegroundColor Red
    exit 1
}
Write-Host "检测到 Python $ver"

# 3) 安装包（--user）
Write-Host "==> pip install --user $PKG"
python -m pip install --user --upgrade $PKG

# 4) 拉取 Camoufox 浏览器内核
Write-Host "==> 拉取 Camoufox 浏览器内核"
python -m camoufox fetch

# 5) 提示 PATH
Write-Host ""
Write-Host "完成。请将用户级脚本目录加入 PATH："
Write-Host "  %APPDATA%\Python\Scripts"
Write-Host ""
Write-Host "使用方式："
Write-Host "  xhs --help            # 命令行"
Write-Host "  xhs-dashboard         # 启动本地后台 http://127.0.0.1:8765"
Write-Host ""
Write-Host "重新安装 / 升级："
Write-Host "  irm https://raw.githubusercontent.com/alanl1234/xiaohongshu-matrices-cli/main/scripts/install.ps1 | iex"
