#!/bin/bash
#
# Builds "iMessage Wrapped.app" and a DMG to put it in.
#
#   ./mac/build.sh
#
# By default the app is ad-hoc signed, which is enough to run on the machine that
# built it. To make something other people can open without a trip through System
# Settings, set SIGN_IDENTITY to a Developer ID Application certificate and notarise
# the DMG afterwards — see mac/README.md.
#
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./mac/build.sh
#
set -euo pipefail

VERSION="${VERSION:-1.0.0}"
APP_NAME="iMessage Wrapped"
BUNDLE_ID="com.henrywhite.imessagewrapped"
MIN_MACOS="13.0"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
DIST="$HERE/dist"
APP="$DIST/$APP_NAME.app"
DMG="$DIST/iMessage-Wrapped-$VERSION.dmg"

PYTHON="${PYTHON:-python3}"

step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$1"; }

rm -rf "$BUILD" "$DIST"
mkdir -p "$BUILD" "$DIST"

# ---------------------------------------------------------------------------------
step "Rendering the app icon"
# ---------------------------------------------------------------------------------
swiftc -O -target "arm64-apple-macos$MIN_MACOS" \
    -o "$BUILD/icongen" "$HERE/tools/IconGen.swift"
"$BUILD/icongen" "$BUILD/AppIcon.iconset" >/dev/null
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

# ---------------------------------------------------------------------------------
step "Bundling the analysis engine (PyInstaller, universal2)"
# ---------------------------------------------------------------------------------
# The engine has no third-party dependencies, so a universal build needs nothing
# beyond a universal Python — which the python.org builds are.
"$PYTHON" -m PyInstaller \
    --onedir --noconfirm --clean --log-level WARN \
    --name wrapped-engine \
    --console \
    --target-arch universal2 \
    --exclude-module tkinter \
    --exclude-module test \
    --distpath "$BUILD/pyi/dist" \
    --workpath "$BUILD/pyi/work" \
    --specpath "$BUILD/pyi" \
    "$HERE/engine/wrapped_engine.py"

# ---------------------------------------------------------------------------------
step "Compiling the Mac app (universal2)"
# ---------------------------------------------------------------------------------
for arch in arm64 x86_64; do
    swiftc -O -target "$arch-apple-macos$MIN_MACOS" \
        -o "$BUILD/app-$arch" "$HERE/app/Sources/"*.swift
done
lipo -create -output "$BUILD/app-universal" "$BUILD/app-arm64" "$BUILD/app-x86_64"

# ---------------------------------------------------------------------------------
step "Assembling $APP_NAME.app"
# ---------------------------------------------------------------------------------
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BUILD/app-universal" "$APP/Contents/MacOS/$APP_NAME"
chmod +x "$APP/Contents/MacOS/$APP_NAME"
cp "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp -R "$BUILD/pyi/dist/wrapped-engine" "$APP/Contents/Resources/engine"

sed -e "s/@VERSION@/$VERSION/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" \
    "$HERE/app/Resources/Info.plist.template" > "$APP/Contents/Info.plist"

printf 'APPL????' > "$APP/Contents/PkgInfo"

# ---------------------------------------------------------------------------------
step "Signing"
# ---------------------------------------------------------------------------------
IDENTITY="${SIGN_IDENTITY:--}"
if [ "$IDENTITY" = "-" ]; then
    echo "   ad-hoc (set SIGN_IDENTITY for a distributable build)"
    SIGN_ARGS=(--force --sign -)
else
    echo "   $IDENTITY"
    SIGN_ARGS=(--force --sign "$IDENTITY" --options runtime --timestamp)
fi

# Sign inside out: every nested Mach-O first, then the bundle that contains them.
# Signing the outside first would be invalidated the moment the inside changed.
while IFS= read -r binary; do
    codesign "${SIGN_ARGS[@]}" "$binary" 2>/dev/null || true
done < <(find "$APP/Contents/Resources/engine" -type f \
    \( -name '*.so' -o -name '*.dylib' -o -perm +111 \) 2>/dev/null)

codesign "${SIGN_ARGS[@]}" "$APP"
codesign --verify --deep --strict "$APP" && echo "   signature verifies"

# ---------------------------------------------------------------------------------
step "Building the DMG"
# ---------------------------------------------------------------------------------
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    -quiet \
    "$DMG"

step "Done"
echo "   app  $APP"
echo "   dmg  $DMG  ($(du -h "$DMG" | cut -f1))"
if [ "$IDENTITY" = "-" ]; then
    cat <<'EOF'

   This build is ad-hoc signed. On another Mac, Gatekeeper will refuse it until the
   user goes to System Settings > Privacy & Security and clicks "Open Anyway".
   To avoid that, rebuild with a Developer ID certificate and notarise the DMG.
EOF
fi
