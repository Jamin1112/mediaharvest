#!/usr/bin/env bash
# mediaharvest 一键安装脚本
#
#   ./setup.sh          安装依赖（含无头浏览器）
#   ./setup.sh --no-browser   不下载浏览器（省 ~100MB，动态页面功能不可用）
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
VENV="$ROOT/.venv"
BROWSERS="$ROOT/.playwright-browsers"

SKIP_BROWSER=0
for arg in "$@"; do
  [ "$arg" = "--no-browser" ] && SKIP_BROWSER=1
done

echo "==> mediaharvest 安装"
echo "    目录: $ROOT"

# --- Python 版本检查 ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误: 未找到 python3，请先安装 Python 3.8+" >&2
  exit 1
fi
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    Python: $PYVER"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || {
  echo "错误: 需要 Python 3.8 及以上（当前 $PYVER）" >&2; exit 1; }

# --- 虚拟环境 ---
if [ ! -d "$VENV" ]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv "$VENV"
fi
PIP="$VENV/bin/python -m pip"
$PIP install --upgrade pip -q

# --- 依赖 ---
echo "==> 安装 Python 依赖"
$PIP install --no-input -q \
  "httpx[http2]" beautifulsoup4 lxml playwright flask yt-dlp m3u8 pycryptodome \
  "tomli>=1.1.0; python_version < '3.11'"

# --- 浏览器（放入项目内，沙箱环境也能用）---
if [ "$SKIP_BROWSER" -eq 0 ]; then
  echo "==> 安装无头浏览器（约 150MB，用于 JS 动态页面）"
  PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" "$VENV/bin/python" -m playwright install chromium
else
  echo "==> 跳过浏览器安装（--no-browser）"
fi

# --- 完成 ---
cat <<EOF

==> 安装完成

  命令行用法:
    ./mh https://example.com                  # 抓取并下载
    ./mh https://example.com --list           # 只看有哪些资源
    ./mh https://example.com -t video -o out  # 只下视频
    ./mh --help                               # 全部参数

  Web 界面:
    ./mh-web                                  # 自动打开 http://127.0.0.1:8848

  环境自检:
    ./mh --selfcheck

  配置下载地址:
    ./mh --set-out-dir ~/Downloads/media       # 存为默认下载地址
    ./mh --show-config                         # 查看当前配置

EOF
