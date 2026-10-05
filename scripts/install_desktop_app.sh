#!/usr/bin/env bash
# Create one double-click AlphaWave app on your Mac Desktop (macOS only).
# Double-click: pulls latest main, starts or restarts the scheduler + dashboard as needed, opens the dashboard.
# Run once:  bash scripts/install_desktop_app.sh   [optional: target folder, default ~/Desktop]
# The apps call the scripts in this repo, so `git pull` updates them; re-run only if you move the repo.
set -eu

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$HOME/Desktop}"
ICON_SRC="$REPO/assets/brand/alphawave-mark-512.png"

make_icns() {  # make_icns <out.icns>  (needs macOS sips + iconutil; skipped otherwise)
  command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1 && [ -f "$ICON_SRC" ] || return 1
  local set; set="$(mktemp -d)/AlphaWave.iconset"; mkdir -p "$set"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s "$ICON_SRC" --out "$set/icon_${s}x${s}.png" >/dev/null
  done
  cp "$set/icon_32x32.png"   "$set/icon_16x16@2x.png"
  cp "$set/icon_64x64.png"   "$set/icon_32x32@2x.png"
  cp "$set/icon_256x256.png" "$set/icon_128x128@2x.png"
  cp "$set/icon_512x512.png" "$set/icon_256x256@2x.png"
  rm -f "$set/icon_64x64.png"
  iconutil -c icns "$set" -o "$1"
}

make_app() {  # make_app "<App Name>" <bundle-id> <repo script name>
  local name="$1" bid="$2" script="$3"
  local app="$DEST/$name.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  printf '%s' "$REPO" > "$app/Contents/Resources/repo_path"

  cat > "$app/Contents/MacOS/run" <<SH
#!/bin/bash
REPO="\$(cat "\$(dirname "\$0")/../Resources/repo_path")"
export ALPHAWAVE_REPO="\$REPO"
exec /bin/bash "\$REPO/scripts/$script"
SH
  chmod +x "$app/Contents/MacOS/run"

  local icon_key=""
  if make_icns "$app/Contents/Resources/AlphaWave.icns"; then
    icon_key="<key>CFBundleIconFile</key><string>AlphaWave</string>"
  else
    echo "Note: icon not generated (needs macOS sips/iconutil); the app still works." >&2
  fi

  cat > "$app/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>$name</string>
<key>CFBundleDisplayName</key><string>$name</string>
<key>CFBundleIdentifier</key><string>$bid</string>
<key>CFBundleExecutable</key><string>run</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
$icon_key
</dict></plist>
PL
  touch "$app"
  echo "Created: $app"
}

make_applet() {  # a real AppleScript applet: launches reliably from Finder (macOS only)
  local app="$DEST/AlphaWave.app" src; src="$(mktemp -d)/AlphaWave.applescript"
  rm -rf "$app"
  cat > "$src" <<AS
on run
	do shell script "mkdir -p ~/Library/Logs; ALPHAWAVE_REPO=" & quoted form of "$REPO" & " /bin/bash " & quoted form of "$REPO/scripts/alphawave_launcher.sh" & " >> ~/Library/Logs/AlphaWave-app.log 2>&1 &"
end run
AS
  osacompile -o "$app" "$src" || return 1
  if make_icns "$app/Contents/Resources/applet.icns"; then
    command -v codesign >/dev/null 2>&1 && codesign --force --deep -s - "$app" >/dev/null 2>&1 || true
  fi
  touch "$app"
  echo "Created: $app"
}

mkdir -p "$DEST"
chmod +x "$REPO/scripts/alphawave_launcher.sh" "$REPO/scripts/alphawave_stop.sh"
rm -rf "$DEST/Stop AlphaWave.app"   # from an earlier version
if command -v osacompile >/dev/null 2>&1 && make_applet; then :; else
  make_app "AlphaWave" "com.alphawave.launcher" alphawave_launcher.sh
fi
echo
echo "Checking your setup..."
ALPHAWAVE_CHECK=1 ALPHAWAVE_NO_PULL=1 bash "$REPO/scripts/alphawave_launcher.sh" || echo "Fix the problem above, then double-click AlphaWave."
echo
echo "Done. Double-click AlphaWave on your Desktop. It also picks up new merges each time."
echo "First launch: if macOS asks, right-click the app > Open once."
