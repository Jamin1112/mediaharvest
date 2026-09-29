#!/usr/bin/env bash
# Create a simple distributable DMG from dist/MediaHarvest.app.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="$ROOT/dist/MediaHarvest.app"
DMG_DIR="$ROOT/dist/dmg"
DMG="$ROOT/dist/MediaHarvest-macOS.dmg"
VOL_NAME="MediaHarvest"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "错误: DMG 需要在 macOS 上制作。" >&2
  exit 1
fi

if [ ! -d "$APP" ]; then
  echo "错误: 未找到 $APP" >&2
  echo "请先运行 ./scripts/build_macos_dist.sh" >&2
  exit 1
fi

rm -rf "$DMG_DIR" "$DMG"
mkdir -p "$DMG_DIR"
cp -R "$APP" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"

echo "==> 创建 DMG"
hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$DMG_DIR" \
  -ov \
  -format UDZO \
  "$DMG"

rm -rf "$DMG_DIR"

echo
echo "==> DMG 已生成"
echo "    $DMG"
