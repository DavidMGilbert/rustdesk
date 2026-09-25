#!/bin/sh
# Build ylts-agent.exe (Windows x64) from Linux, macOS or Windows (Git Bash).
# Needs Go 1.24+; the resource step needs mingw-w64 windres (optional: skipped if missing).
set -eu
cd "$(dirname "$0")"
VERSION=${VERSION:-1.0.0}
if command -v x86_64-w64-mingw32-windres >/dev/null 2>&1; then
  V=$(echo "$VERSION" | tr . ,)
  sed -e "s/1,0,0,0/$V,0/g" -e "s/\"1.0.0\"/\"$VERSION\"/g" agent.rc > agent.gen.rc
  x86_64-w64-mingw32-windres -O coff -o rsrc_windows_amd64.syso agent.gen.rc
  rm -f agent.gen.rc
else
  echo "windres not found: building without icon/version resource" >&2
fi
mkdir -p dist
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath \
  -ldflags "-s -w -H=windowsgui -X main.Version=$VERSION" -o dist/ylts-agent.exe .
echo "built dist/ylts-agent.exe ($VERSION)"
