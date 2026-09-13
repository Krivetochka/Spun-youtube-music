#!/usr/bin/env bash
#
# Build a mostly self-contained Spun AppImage.
#
# Run inside a clean Ubuntu 22.04 environment (e.g. a distrobox) so the binary
# links an old glibc and runs on most current distributions. The AppImage
# bundles Qt (with the FFmpeg multimedia backend), the app, and the full
# optional YouTube runtime: a standalone Python with ytmusicapi + yt-dlp, the
# bgutil PO-token provider, and the Node and Deno binaries they need. The only
# things a user still needs are a normal desktop system and, for the YouTube
# account features, a signed-in browser to read cookies from.
#
# Usage:  packaging/appimage/build.sh
# Output: <workdir>/Spun-x86_64.AppImage  (workdir defaults to ~/spun-appimage-build)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WS="${SPUN_APPIMAGE_WORKDIR:-$HOME/spun-appimage-build}"
QT_VER="${SPUN_QT_VERSION:-6.8.2}"
DL="$WS/downloads"
APPDIR="$WS/AppDir"
mkdir -p "$WS" "$DL"

log() { printf '\n=== %s ===\n' "$*"; }

# --------------------------------------------------------------------------
log "System build dependencies"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    build-essential cmake ninja-build git wget curl file patchelf unzip \
    libgl1-mesa-dev libglu1-mesa-dev libxkbcommon-dev libxkbcommon-x11-dev \
    libvulkan-dev libfontconfig1-dev libfreetype6-dev zlib1g-dev \
    libxcb1-dev libx11-xcb-dev libxcb-cursor0 libegl1-mesa-dev \
    libpulse-dev libasound2-dev \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 \
    libxcb-xinerama0 libxcb-xkb1 libxcb-util1 libxkbcommon-x11-0 \
    libwayland-dev \
    python3-pip python3-venv >/dev/null
fi

# --------------------------------------------------------------------------
log "Qt $QT_VER (via aqtinstall)"
QTDIR="$WS/qt/$QT_VER/gcc_64"
# Require a real module (not just qmake) so a half-finished download re-runs.
if [ ! -e "$QTDIR/lib/libQt6Quick3D.so.6" ] || [ ! -x "$QTDIR/bin/qsb" ]; then
  pip3 install -q --user aqtinstall
  # --archives limits the download to what Spun needs; qttools/qttranslations
  # are skipped (and were the flaky part of a full install).
  python3 -m aqt install-qt linux desktop "$QT_VER" linux_gcc_64 \
    -m qtmultimedia qtquick3d qtshadertools -O "$WS/qt"
fi
export PATH="$QTDIR/bin:$PATH"
export LD_LIBRARY_PATH="$QTDIR/lib:${LD_LIBRARY_PATH:-}"

# --------------------------------------------------------------------------
log "TagLib 2.x (from source; not in Ubuntu 22.04)"
TAGPFX="$WS/taglib-prefix"
if [ ! -f "$TAGPFX/lib/libtag.so" ]; then
  # Clone with submodules so the bundled utf8cpp is present (the release
  # tarball omits it, and Ubuntu 22.04 has no libutfcpp-dev).
  rm -rf "$WS/taglib-src" "$WS/taglib-build"
  git clone --recursive --depth 1 --branch v2.0.2 \
    https://github.com/taglib/taglib.git "$WS/taglib-src"
  cmake -S "$WS/taglib-src" -B "$WS/taglib-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
    -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_INSTALL_PREFIX="$TAGPFX" >/dev/null
  ninja -C "$WS/taglib-build" install >/dev/null
fi
export PKG_CONFIG_PATH="$TAGPFX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# --------------------------------------------------------------------------
log "Build Spun"
cmake -S "$SRC" -B "$WS/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$QTDIR;$TAGPFX" >/dev/null
ninja -C "$WS/build" spun

# --------------------------------------------------------------------------
log "Node + Deno binaries (cached in the work tree)"
if [ ! -x "$WS/node/bin/node" ]; then
  NODE_VER="$(curl -fsSL https://nodejs.org/dist/index.json | grep -oE '"v22\.[0-9]+\.[0-9]+"' | head -1 | tr -d '"')"
  [ -n "$NODE_VER" ] || { echo "Could not determine a Node v22 version" >&2; exit 1; }
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" -o "$DL/node.tar.xz"
  rm -rf "$WS/node"; mkdir -p "$WS/node"
  tar -xf "$DL/node.tar.xz" -C "$WS/node" --strip-components=1
fi
if [ ! -x "$WS/deno/deno" ]; then
  curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" -o "$DL/deno.zip"
  rm -rf "$WS/deno"; mkdir -p "$WS/deno"; unzip -oq "$DL/deno.zip" -d "$WS/deno"
fi
export PATH="$WS/node/bin:$PATH"   # npm/npx for building bgutil below

# --------------------------------------------------------------------------
log "Standalone Python + ytmusicapi/yt-dlp/bgutil plugin (cached)"
PYROOT="$WS/python"
if [ ! -x "$PYROOT/bin/python3" ]; then
  PBS_TAG="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest | grep -oE '"tag_name": *"[^"]+"' | head -1 | grep -oE '[0-9]+')"
  # The asset URL encodes the '+' as %2B in the JSON, and there is also an
  # install_only_stripped variant to avoid.
  PBS_URL="$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$PBS_TAG" \
    | grep -oE 'https://[^"]*cpython-3\.12\.[0-9]+(%2B|\+)[0-9]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz' | head -1)"
  [ -n "$PBS_URL" ] || { echo "Could not find a python-build-standalone asset URL" >&2; exit 1; }
  curl -fsSL "$PBS_URL" -o "$DL/python.tar.gz"
  rm -rf "$PYROOT"; mkdir -p "$PYROOT"
  tar -xf "$DL/python.tar.gz" -C "$PYROOT" --strip-components=1
fi
if ! "$PYROOT/bin/python3" -c 'import ytmusicapi, yt_dlp' 2>/dev/null; then
  "$PYROOT/bin/python3" -m pip install -q --upgrade pip
  "$PYROOT/bin/python3" -m pip install -q -r "$SRC/helper/requirements.txt"
fi

# --------------------------------------------------------------------------
log "bgutil PO-token provider (built in this environment, cached)"
BGSRC="$WS/bgutil"
if [ ! -f "$BGSRC/server/build/generate_once.js" ]; then
  rm -rf "$BGSRC"
  git clone --depth 1 --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$BGSRC"
  ( cd "$BGSRC/server" && npm install --no-audit --no-fund && npx tsc )
fi

# --------------------------------------------------------------------------
log "AppDir + Qt (linuxdeploy runs before the runtime is copied in, so it only
    processes the Spun binary and Qt, not the bundled interpreters)"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp "$WS/build/spun" "$APPDIR/usr/bin/spun"
fetch_tool() { # name url
  local out="$DL/$1"
  [ -x "$out" ] || { curl -fsSL "$2" -o "$out"; chmod +x "$out"; }
  echo "$out"
}
LD="$(fetch_tool linuxdeploy https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage)"
fetch_tool linuxdeploy-plugin-qt https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/continuous/linuxdeploy-plugin-qt-x86_64.AppImage >/dev/null
export PATH="$DL:$PATH"   # linuxdeploy locates its qt plugin here
export QMAKE="$QTDIR/bin/qmake6"
export QML_SOURCES_PATHS="$SRC/qml"
export EXTRA_QT_MODULES="multimedia;quick3d;svg;quickcontrols2"
# Bundle the Wayland platform plugins alongside xcb so Qt runs natively on
# Wayland sessions (falling back to XWayland/xcb elsewhere).
export EXTRA_PLATFORM_PLUGINS="libqwayland-egl.so;libqwayland-generic.so"
export APPIMAGE_EXTRACT_AND_RUN=1
"$LD" --appimage-extract-and-run \
  --appdir "$APPDIR" \
  --executable "$APPDIR/usr/bin/spun" \
  --desktop-file "$SRC/packaging/appimage/spun.desktop" \
  --icon-file "$SRC/assets/spun-icon.png" \
  --icon-filename spun \
  --plugin qt

# linuxdeploy-plugin-qt does not know the Wayland plugin categories, so copy
# the shell-integration (xdg-shell), client-side decorations and EGL graphics
# integration by hand and point their RPATH at the bundled Qt libraries.
for cat in wayland-shell-integration wayland-decoration-client \
           wayland-graphics-integration-client; do
  [ -d "$QTDIR/plugins/$cat" ] || continue
  mkdir -p "$APPDIR/usr/plugins/$cat"
  cp -a "$QTDIR/plugins/$cat/"*.so "$APPDIR/usr/plugins/$cat/"
  for so in "$APPDIR/usr/plugins/$cat/"*.so; do
    patchelf --set-rpath '$ORIGIN/../../lib' "$so" || true
  done
done

# --------------------------------------------------------------------------
log "Add the YouTube runtime into the AppDir"
mkdir -p "$APPDIR/usr/share/spun" "$APPDIR/usr/helper"
cp "$SRC/helper/youtube.py" "$APPDIR/usr/helper/youtube.py"
cp "$SRC/assets/First-Light.flac" "$APPDIR/usr/share/spun/First-Light.flac"
cp "$WS/node/bin/node" "$APPDIR/usr/bin/node"
cp "$WS/deno/deno" "$APPDIR/usr/bin/deno"
cp -a "$PYROOT" "$APPDIR/usr/python"
cp -a "$BGSRC" "$APPDIR/usr/bgutil"

# Bundle a CA certificate bundle. Qt's TLS (album artwork over HTTPS) otherwise
# looks for certificates at a compiled-in path that differs per distribution
# (Debian/Ubuntu /etc/ssl/certs, Fedora /etc/pki/...), so it fails on others.
# SSL_CERT_FILE in AppRun points OpenSSL at this bundle everywhere.
cp "$PYROOT"/lib/python*/site-packages/certifi/cacert.pem \
   "$APPDIR/usr/share/spun/cacert.pem" 2>/dev/null \
  || cp /etc/ssl/certs/ca-certificates.crt "$APPDIR/usr/share/spun/cacert.pem"

# linuxdeploy leaves AppRun as a symlink to the binary, which sets neither
# LD_LIBRARY_PATH (so the bundled Qt is used instead of the host's) nor our
# runtime env (so the bundled Python/Node/Deno are found on any machine).
# Replace it with a real launcher.
rm -f "$APPDIR/AppRun"
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export APPDIR="${APPDIR:-$HERE}"
export LD_LIBRARY_PATH="$APPDIR/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-$APPDIR/usr/share/spun/cacert.pem}"
export SPUN_YOUTUBE_PYTHON="$APPDIR/usr/python/bin/python3"
export SPUN_YOUTUBE_HELPER="$APPDIR/usr/helper/youtube.py"
export SPUN_YOUTUBE_POT_SCRIPT="$APPDIR/usr/bgutil/server/build/generate_once.js"
export SPUN_DEMO_FILE="$APPDIR/usr/share/spun/First-Light.flac"
export PATH="$APPDIR/usr/bin:$PATH"
exec "$APPDIR/usr/bin/spun" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# --------------------------------------------------------------------------
log "Pack the AppImage (appimagetool)"
AT="$(fetch_tool appimagetool https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage)"
OUT="$WS/Spun-x86_64.AppImage"
ARCH=x86_64 "$AT" --appimage-extract-and-run "$APPDIR" "$OUT"
log "Done: $OUT"
ls -lh "$OUT"
