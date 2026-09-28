#!/usr/bin/env bash
# Build a lightweight macOS .app wrapper for the local mediaharvest web UI.
#
# The generated app points at this checkout and uses the project's .venv.
# Share the repository plus setup instructions, or ask users to run this
# script on their own machine after ./setup.sh.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP_NAME="MediaHarvest"
APP_DIR="$ROOT/dist/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
ICONSET="$RESOURCES/AppIcon.iconset"
LOG_DIR="$ROOT/logs"

echo "==> Building $APP_NAME.app"
echo "    Project: $ROOT"

rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES" "$ICONSET" "$LOG_DIR"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleDisplayName</key>
  <string>MediaHarvest</string>
  <key>CFBundleExecutable</key>
  <string>MediaHarvest</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>local.mediaharvest.desktop</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>MediaHarvest</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.1.0</string>
  <key>CFBundleVersion</key>
  <string>1.1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>10.15</string>
  <key>LSUIElement</key>
  <false/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$MACOS/$APP_NAME" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$ROOT"
VENV_PY="\$PROJECT_ROOT/.venv/bin/python"
LOG_DIR="\$PROJECT_ROOT/logs"
LOG_FILE="\$LOG_DIR/mediaharvest-desktop.log"
URL="http://127.0.0.1:8848"

mkdir -p "\$LOG_DIR"

fail_dialog() {
  local message="\$1"
  /usr/bin/osascript - "\$message" <<'OSA' >/dev/null 2>&1 || true
on run argv
  display dialog (item 1 of argv) buttons {"好"} default button "好" with icon caution
end run
OSA
}

if [ ! -x "\$VENV_PY" ]; then
  fail_dialog "MediaHarvest 尚未安装依赖。请先在项目目录运行 ./setup.sh，然后重新打开 App。"
  exit 1
fi

if "\$VENV_PY" - <<'PY' >/dev/null 2>&1
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8848/api/status", timeout=0.4).read()
except Exception:
    raise SystemExit(1)
PY
then
  /usr/bin/open "\$URL"
  exit 0
fi

cd "\$PROJECT_ROOT"
echo "==> \$(date '+%Y-%m-%d %H:%M:%S') starting MediaHarvest" >> "\$LOG_FILE"
exec "\$VENV_PY" -m mediaharvest.web >> "\$LOG_FILE" 2>&1
LAUNCHER
chmod +x "$MACOS/$APP_NAME"

if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  SRC_ICON="$ROOT/mediaharvest/static/logo.png"
  cp "$SRC_ICON" "$RESOURCES/AppIcon.png"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$SRC_ICON" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    if [ "$double" -le 1024 ]; then
      sips -z "$double" "$double" "$SRC_ICON" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    fi
  done
  if iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns" >/dev/null 2>&1; then
    rm -rf "$ICONSET"
  else
    echo "    Warning: iconutil could not create AppIcon.icns; using AppIcon.png fallback."
    rm -rf "$ICONSET"
  fi
else
  cp "$ROOT/mediaharvest/static/logo.png" "$RESOURCES/AppIcon.png"
fi

echo
echo "==> Done"
echo "    $APP_DIR"
echo
echo "Double-click the app, or run:"
echo "    open \"$APP_DIR\""
