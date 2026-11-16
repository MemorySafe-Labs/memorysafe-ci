#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SOURCE_ROOT="${SCRIPT_DIR:h}"
# Inputs live in the repository and outputs are chosen by the caller. Deriving
# either from the repository's parent directories broke the moment the project
# moved, and the legal documents and icon stayed behind in the old location.
LEGAL_DIR="$SOURCE_ROOT/legal"
ICON_PNG="$SOURCE_ROOT/assets/memorysafe-icon.png"
OUTPUT_DIR="${1:-${MEMORYSAFE_OUTPUT_DIR:-$SOURCE_ROOT/dist}}"
STAGING_ROOT="$(/usr/bin/mktemp -d /private/tmp/memorysafe-installer-build.XXXXXX)"
APP_NAME="MemorySafe Beta Installer.app"
APP="$STAGING_ROOT/$APP_NAME"
CONTENTS="$APP/Contents"
PAYLOAD="$CONTENTS/Resources/payload"
ICONSET="$STAGING_ROOT/MemorySafe.iconset"

trap '/bin/rm -rf "$STAGING_ROOT"' EXIT
/bin/mkdir -p "$CONTENTS/MacOS" "$PAYLOAD/src" "$PAYLOAD/scripts" "$PAYLOAD/bin" "$PAYLOAD/legal" "$ICONSET" "$OUTPUT_DIR"

/bin/cp "$SOURCE_ROOT/packaging/macos/Installer-Info.plist" "$CONTENTS/Info.plist"
/bin/cp "$SOURCE_ROOT/packaging/macos/installer_launcher.sh" "$CONTENTS/MacOS/MemorySafe Beta Installer"
/bin/chmod 755 "$CONTENTS/MacOS/MemorySafe Beta Installer"
/bin/cp -R "$SOURCE_ROOT/src/." "$PAYLOAD/src/"
/bin/rm -rf "$PAYLOAD/src/memorysafe_chatgpt/__pycache__" "$PAYLOAD/src/memorysafe_chatgpt_connector.egg-info"
/bin/cp "$SOURCE_ROOT/pyproject.toml" "$PAYLOAD/pyproject.toml"
/bin/cp "$SOURCE_ROOT/scripts/install_macos.py" "$PAYLOAD/scripts/"
/bin/cp "$SOURCE_ROOT/scripts/run_mcp_service.sh" "$PAYLOAD/scripts/"
/bin/cp "$SOURCE_ROOT/scripts/run_setup_service.sh" "$PAYLOAD/scripts/"
/bin/cp "$SOURCE_ROOT/scripts/run_tunnel_service.sh" "$PAYLOAD/scripts/"
/bin/cp "$SOURCE_ROOT/scripts/write_runtime_config.py" "$PAYLOAD/scripts/"
/bin/chmod 755 "$PAYLOAD/scripts/"*.sh
/bin/cp "$SOURCE_ROOT/bin/tunnel-client" "$PAYLOAD/bin/"
/bin/chmod 755 "$PAYLOAD/bin/tunnel-client"
/bin/cp "$LEGAL_DIR/MemorySafe_Beta_Terms_of_Use.md" "$PAYLOAD/legal/"
/bin/cp "$LEGAL_DIR/MemorySafe_Beta_Privacy_Policy.md" "$PAYLOAD/legal/"

ICON_MASTER="$STAGING_ROOT/MemorySafe-1024.png"
/usr/bin/sips -z 1024 1024 "$ICON_PNG" --out "$ICON_MASTER" >/dev/null
/usr/bin/sips -z 16 16 "$ICON_MASTER" --out "$ICONSET/icon_16x16.png" >/dev/null
/usr/bin/sips -z 32 32 "$ICON_MASTER" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
/usr/bin/sips -z 32 32 "$ICON_MASTER" --out "$ICONSET/icon_32x32.png" >/dev/null
/usr/bin/sips -z 64 64 "$ICON_MASTER" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
/usr/bin/sips -z 128 128 "$ICON_MASTER" --out "$ICONSET/icon_128x128.png" >/dev/null
/usr/bin/sips -z 256 256 "$ICON_MASTER" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
/usr/bin/sips -z 256 256 "$ICON_MASTER" --out "$ICONSET/icon_256x256.png" >/dev/null
/usr/bin/sips -z 512 512 "$ICON_MASTER" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
/usr/bin/sips -z 512 512 "$ICON_MASTER" --out "$ICONSET/icon_512x512.png" >/dev/null
/bin/cp "$ICON_MASTER" "$ICONSET/icon_512x512@2x.png"
/usr/bin/python3 "$SOURCE_ROOT/scripts/build_icns.py" "$ICONSET" "$CONTENTS/Resources/MemorySafe.icns"
/bin/cp "$CONTENTS/Resources/MemorySafe.icns" "$PAYLOAD/MemorySafe.icns"

/usr/bin/xattr -cr "$APP"
/usr/bin/codesign --force --deep --sign - --identifier ca.memorysafe.beta.installer "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/bin/rm -rf "$OUTPUT_DIR/$APP_NAME"
/bin/cp -R "$APP" "$OUTPUT_DIR/$APP_NAME"
/bin/rm -f "$OUTPUT_DIR/MemorySafe-Beta-Installer-macOS-Intel.zip"
/usr/bin/ditto -c -k --norsrc --noextattr --noacl --nopersistRootless --keepParent "$APP" "$OUTPUT_DIR/MemorySafe-Beta-Installer-macOS-Intel.zip"

echo "$OUTPUT_DIR/$APP_NAME"
echo "$OUTPUT_DIR/MemorySafe-Beta-Installer-macOS-Intel.zip"
