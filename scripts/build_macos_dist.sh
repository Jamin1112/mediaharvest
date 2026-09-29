#!/usr/bin/env bash
# Build a standalone macOS .app with PyInstaller.
#
# Output:
#   dist/MediaHarvest.app
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
VENV="$ROOT/.venv-build"
BUILD_DIR="$ROOT/build/pyinstaller"
DIST_DIR="$ROOT/dist"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "错误: macOS .app 需要在 macOS 上构建。" >&2
  exit 1
fi

echo "==> MediaHarvest standalone macOS build"
echo "    Project: $ROOT"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> 创建打包虚拟环境 .venv-build"
  "$PY" -m venv "$VENV"
fi

PIP="$VENV/bin/python -m pip"
"$VENV/bin/python" -m pip install --upgrade pip

echo "==> 安装打包依赖"
$PIP install --upgrade pyinstaller
$PIP install --upgrade -e ".[music]"

if [ ! -d "$ROOT/.playwright-browsers" ] || ! find "$ROOT/.playwright-browsers" -maxdepth 1 -type d -name 'chromium*' | grep -q .; then
  echo "==> 安装 Chromium 到 .playwright-browsers"
  PLAYWRIGHT_BROWSERS_PATH="$ROOT/.playwright-browsers" "$VENV/bin/python" -m playwright install chromium
else
  echo "==> 复用已有 .playwright-browsers"
fi

echo "==> 运行 PyInstaller"
rm -rf "$BUILD_DIR" "$DIST_DIR/MediaHarvest" "$DIST_DIR/MediaHarvest.app"
"$VENV/bin/python" -m PyInstaller \
  --clean \
  --noconfirm \
  --workpath "$BUILD_DIR" \
  --distpath "$DIST_DIR" \
  "$ROOT/packaging/MediaHarvest.spec"

if [ -d "$ROOT/.playwright-browsers" ]; then
  echo "==> 复制 Chromium 浏览器资源到 App"
  APP_RESOURCES="$DIST_DIR/MediaHarvest.app/Contents/Resources"
  mkdir -p "$APP_RESOURCES"
  rm -rf "$APP_RESOURCES/.playwright-browsers"
  cp -R "$ROOT/.playwright-browsers" "$APP_RESOURCES/.playwright-browsers"
fi

echo
echo "==> 构建完成"
echo "    $DIST_DIR/MediaHarvest.app"
echo
echo "测试运行:"
echo "    open \"$DIST_DIR/MediaHarvest.app\""
echo
echo "制作 DMG:"
echo "    ./scripts/make_dmg.sh"
