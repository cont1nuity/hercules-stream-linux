#!/bin/bash
# Build the Hercules Stream for Linux AppImage.
#
# Self-contained result: CPython (python-appimage manylinux build) + pyusb + Pillow +
# libusb-1.0 + the runtime payload (src/, icons/ pre-rendered to 32x32 PNG, fonts/,
# config.example.toml, udev rule). The target host needs NO python install — only the
# PipeWire/Pulse tools (pactl, parec) and, for device access, the udev rule.
# The bundled CPython already ships Tkinter + Tcl/Tk (libtcl/libtk + the tcl8.6/tk8.6 script
# dirs), so the graphical config editor (src/configui.py, launched from the tray) runs from
# the AppImage too. python-appimage's own AppRun set TCL_LIBRARY/TK_LIBRARY; ours replaces
# it, so step 6 re-exports them (no size cost — the files are already in the bundle).
#
# Build-host requirements: bash, curl, rsvg-convert (icon pre-render; build time only).
# glibc note: the bundled CPython is manylinux2014 (old glibc), but libusb-1.0.so.0 is
# copied from the BUILD host — build releases on the oldest supported distro (CI runs
# ubuntu LTS for this reason).
#
# Usage:   packaging/build-appimage.sh [VERSION] [--set KEY=VALUE ...] [--enable-stream200]
#          VERSION=1.0.0 packaging/build-appimage.sh
#          default VERSION: `git describe --tags` (CI builds on a tag get the tag name), else "dev"
# Output:  dist/Hercules-Stream-Linux-<VERSION>-x86_64.AppImage
#          VERSION is also stamped into the payload's ui.py (tray/help) and the
#          desktop entry's X-AppImage-Version.
#
# Internal build overrides (--set): bake a src/build_overrides.toml into the payload that
# OVERRIDES the user's config.toml at runtime (see src/features.py). KEY is a dotted path;
# VALUE is parsed as bool/int/float, else string. Repeatable. Use this to ship a build with an
# experimental feature wired on:
#          --set features.stream200=true     turn the Stream 200 XLR backend ON (overrides config)
#          --enable-stream200                shorthand for --set features.stream200=true
#          --set brightness=80 --set ui.vu_gain=2   (pin arbitrary settings)
# Stock public builds pass none — every feature flag then resolves to its code default (OFF).
# Downloads are cached in build/cache/ — repeat builds run offline.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# args: an optional positional VERSION (or the VERSION env var) + repeatable --set / shorthands
_env_version="${VERSION:-}"
VERSION=""
SETS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --set)   [ $# -ge 2 ] || { echo "ERROR: --set needs KEY=VALUE"; exit 1; }
                 SETS+=("$2"); shift 2 ;;
        --set=*) SETS+=("${1#--set=}"); shift ;;
        --enable-stream200) SETS+=("features.stream200=true"); shift ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        --*) echo "ERROR: unknown flag $1 (see --help)"; exit 1 ;;
        *)   [ -z "$VERSION" ] || { echo "ERROR: unexpected argument $1"; exit 1; }
             VERSION="$1"; shift ;;
    esac
done
VERSION="${VERSION:-$_env_version}"
if [ -z "$VERSION" ]; then
    VERSION=$(git -C "$ROOT" describe --tags --dirty 2>/dev/null || echo dev)
fi
ARCH=x86_64
PYTAG=python3.12                 # python-appimage release tag (3.11+ needed for tomllib)
BUILD="$ROOT/build/appimage"
CACHE="$ROOT/build/cache"
DIST="$ROOT/dist"
APPDIR="$BUILD/AppDir"

mkdir -p "$BUILD" "$CACHE" "$DIST"
rm -rf "$APPDIR"

fetch() {  # fetch <url> <dest> — cached
    [ -f "$2" ] && return 0
    echo ">> fetching $(basename "$2")"
    curl -fL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}

# 1) toolchain: python-appimage (relocatable manylinux CPython) + appimagetool
# (consumers below read their whole input — early-exit filters like `head`/`awk exit`
# in a pipeline trip pipefail via SIGPIPE)
REL_JSON=$(curl -fsSL "https://api.github.com/repos/niess/python-appimage/releases/tags/$PYTAG")
PY_URL=$(printf '%s' "$REL_JSON" \
         | grep -o "\"browser_download_url\": *\"[^\"]*manylinux2014_$ARCH\.AppImage\"" \
         | awk -F'"' 'NR==1{print $4}')
[ -n "$PY_URL" ] || { echo "ERROR: no manylinux2014 $PYTAG asset found on python-appimage"; exit 1; }
PY_AI="$CACHE/$(basename "$PY_URL")"
fetch "$PY_URL" "$PY_AI"
AIT="$CACHE/appimagetool-$ARCH.AppImage"
fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" "$AIT"
chmod +x "$PY_AI" "$AIT"

# 2) unpack the python AppImage -> AppDir (--appimage-extract needs no FUSE)
(cd "$BUILD" && "$PY_AI" --appimage-extract >/dev/null)
mv "$BUILD/squashfs-root" "$APPDIR"
PY="$(find "$APPDIR/usr/bin" -name 'python3.[0-9]*' | sort | head -1)"
[ -x "$PY" ] || { echo "ERROR: bundled python not found in AppDir"; exit 1; }
echo ">> bundled interpreter: $("$PY" --version)"

# 3) python deps into the bundle (manylinux wheels — self-contained)
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet "pyusb>=1.2" pillow dbus-next certifi   # dbus-next: tray (SNI); certifi: CA store (manylinux CPython ships none) for the tray's HTTPS update check

# 4) runtime payload
PAY="$APPDIR/opt/hercules-stream"
mkdir -p "$PAY"
cp -r "$ROOT/src" "$PAY/src"
find "$PAY/src" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -f "$PAY/src/build_overrides.toml"   # never ship a dev-local override; --set re-bakes it below
sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$PAY/src/ui.py"   # tray/help version
cp -r "$ROOT/fonts" "$PAY/fonts"
cp "$ROOT/config.example.toml" "$ROOT/99-hercules-stream.rules" "$PAY/"
[ -f "$ROOT/LICENSE" ] && cp "$ROOT/LICENSE" "$PAY/"
# icons: pre-render the SVG set to 32x32 PNG (drops the runtime rsvg-convert dependency;
# the icon stem index resolves either extension, so configs keep using bare names)
mkdir -p "$PAY/icons"
for svg in "$ROOT"/icons/*.svg; do
    rsvg-convert -w 32 -h 32 "$svg" -o "$PAY/icons/$(basename "${svg%.svg}").png"
done
cp "$ROOT/icons/README.md" "$PAY/icons/"

# 4b) internal build overrides (optional). Each --set KEY=VALUE pins a setting that OVERRIDES
#     the user's config.toml at runtime (e.g. --set features.stream200=true ships the
#     experimental Stream 200 XLR backend enabled). Baked into the read-only payload as
#     src/build_overrides.toml and consumed by src/features.py. Stock builds pass none.
if [ ${#SETS[@]} -gt 0 ]; then
    "$PY" - "$PAY/src/build_overrides.toml" "${SETS[@]}" <<'PYGEN'
import sys
out, pairs = sys.argv[1], sys.argv[2:]

def parse(v):
    lv = v.lower()
    if lv in ("true", "false"):
        return lv == "true"
    try: return int(v)
    except ValueError: pass
    try: return float(v)
    except ValueError: pass
    return v

tree = {}
for pr in pairs:
    if "=" not in pr:
        sys.exit("build --set: bad %r (need KEY=VALUE)" % pr)
    key, _, val = pr.partition("=")
    node = tree
    parts = key.strip().split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = parse(val.strip())

def fmt(v):
    if isinstance(v, bool):       return "true" if v else "false"
    if isinstance(v, (int, float)): return repr(v)
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')

def dump(d, prefix=""):
    scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
    tables  = [(k, v) for k, v in d.items() if isinstance(v, dict)]
    blocks = []
    if scalars:
        blocks.append("\n".join("%s = %s" % (k, fmt(v)) for k, v in scalars))
    for k, v in tables:
        name = "%s.%s" % (prefix, k) if prefix else k
        body = dump(v, name)
        blocks.append("[%s]\n%s" % (name, body) if body else "[%s]" % name)
    return "\n\n".join(b for b in blocks if b)

with open(out, "w") as f:
    f.write("# Build-baked internal overrides (generated by build-appimage.sh --set).\n")
    f.write("# These OVERRIDE the user's config.toml at runtime — see src/features.py.\n\n")
    f.write(dump(tree) + "\n")
print(">> baked src/build_overrides.toml from: %s" % ", ".join(pairs))
PYGEN
fi

# 5) libusb from the build host (its own deps — libudev, libc — resolve on the target)
LIBUSB=$(ldconfig -p | awk '/libusb-1\.0\.so\.0 /{if (!l) l=$NF} END{print l}')
[ -n "$LIBUSB" ] || { echo "ERROR: host libusb-1.0.so.0 not found (install libusb)"; exit 1; }
cp -L "$LIBUSB" "$APPDIR/usr/lib/libusb-1.0.so.0"

# 6) AppRun: exec the bundled interpreter DIRECTLY (python-appimage's own AppRun calls
#    python without exec, leaving a bash wrapper alive — two processes for one daemon).
#    The bundled libusb is pinned via HERCULES_STREAM_LIBUSB (src/usbdev.py) instead of
#    LD_LIBRARY_PATH so spawned host tools (pactl/parec) keep resolving host libraries.
#    APPDIR is exported for paths.py's packaged-run detection when running extracted.
#    NOTE: usr/bin/python3.* in python-appimage is a bash WRAPPER SCRIPT (calls python
#    without exec) — AppRun must exec the real ELF under opt/ or the wrapper lingers.
PYELF=$(find "$APPDIR/opt" -path '*/bin/python3.[0-9]*' -type f | awk 'NR==1')
[ -x "$PYELF" ] || { echo "ERROR: bundled python ELF not found under opt/"; exit 1; }
PYREL="${PYELF#"$APPDIR"/}"
# CA store: the manylinux CPython ships no system certs, so HTTPS (the tray's update check)
# fails CERTIFICATE_VERIFY_FAILED. certifi (installed in step 3) provides the bundle; point
# SSL_CERT_FILE at the in-mount copy so Python's default ssl context finds it.
CERT_ABS=$("$PY" -c 'import certifi; print(certifi.where())')
CERT_REL="${CERT_ABS#"$APPDIR"/}"
[ -f "$APPDIR/$CERT_REL" ] || { echo "ERROR: certifi cacert.pem not found in bundle"; exit 1; }
# Tcl/Tk script libraries: needed by Tk() for the config editor (configui.py). The bundle
# ships them; detect their location (relative to APPDIR) so a python-appimage layout change
# is tracked and so the GUI gracefully disables (tray falls back to raw editing) if absent.
TCL_EXPORTS=""
_initcl=$(find "$APPDIR/usr/share" "$APPDIR/usr/lib" -name init.tcl -path '*tcl8*' 2>/dev/null | awk 'NR==1' || true)
_tktcl=$(find "$APPDIR/usr/share" "$APPDIR/usr/lib" -name tk.tcl -path '*tk8*' 2>/dev/null | awk 'NR==1' || true)
if [ -n "$_initcl" ] && [ -n "$_tktcl" ]; then
    TCL_REL="${_initcl%/init.tcl}"; TCL_REL="${TCL_REL#"$APPDIR"/}"
    TK_REL="${_tktcl%/tk.tcl}";     TK_REL="${TK_REL#"$APPDIR"/}"
    TCL_EXPORTS=$(printf 'export TCL_LIBRARY="$APPDIR/%s"\nexport TK_LIBRARY="$APPDIR/%s"\nexport TKPATH="$APPDIR/%s"' \
                  "$TCL_REL" "$TK_REL" "$TK_REL")
    echo ">> tkinter present — config GUI enabled (TCL_LIBRARY=$TCL_REL)"
else
    echo ">> NOTE: no Tcl/Tk in the bundle — config GUI will fall back to raw-file editing"
fi
rm -f "$APPDIR/AppRun"
cat > "$APPDIR/AppRun" <<EOF
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\$0")")"
export APPDIR="\${APPDIR:-\$HERE}"
export HERCULES_STREAM_LIBUSB="\$HERE/usr/lib/libusb-1.0.so.0"
export SSL_CERT_FILE="\${SSL_CERT_FILE:-\$HERE/$CERT_REL}"
$TCL_EXPORTS
exec "\$HERE/$PYREL" "\$HERE/opt/hercules-stream/src/ui.py" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

# 7) desktop entry + icon (appimagetool wants exactly one .desktop + icon at the root)
rm -f "$APPDIR"/*.desktop "$APPDIR"/.DirIcon "$APPDIR"/python*.png "$APPDIR"/python*.svg
rm -rf "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo"
cat > "$APPDIR/hercules-stream.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Hercules Stream for Linux
Comment=Hercules Stream controller (LCD pages + knobs/buttons -> PipeWire)
Exec=hercules-stream
Icon=hercules-stream
Terminal=false
Categories=AudioVideo;Audio;Mixer;
X-AppImage-Version=$VERSION
EOF
rsvg-convert -w 256 -h 256 "$ROOT/icons/hercules.svg" -o "$APPDIR/hercules-stream.png"
ln -sf hercules-stream.png "$APPDIR/.DirIcon"

# 8) pack — embed AppImage update-information so AppImageUpdate can delta-update in place from the
#    latest GitHub release. appimagetool writes the .zsync as <basename>.zsync into its CWD (NOT
#    next to $OUT), so run it from $DIST to keep both artifacts together there. zsyncmake (apt
#    'zsync') must be on PATH for the .zsync — the release workflow installs it; a local build
#    without it still embeds the update-info but skips the .zsync.
OUT="$DIST/Hercules-Stream-Linux-$VERSION-$ARCH.AppImage"
UPDATE_INFO="gh-releases-zsync|cont1nuity|hercules-stream-linux|latest|Hercules-Stream-Linux-*-$ARCH.AppImage.zsync"
rm -f "$OUT" "$OUT.zsync"     # unlink first: overwriting a RUNNING AppImage fails with ETXTBSY
( cd "$DIST" && ARCH=$ARCH "$AIT" --appimage-extract-and-run -u "$UPDATE_INFO" "$APPDIR" "$OUT" )
[ -f "$OUT.zsync" ] || echo ">> note: no .zsync produced (install 'zsync' to enable delta updates)"
echo ""
echo ">> built $OUT"
echo ">> first run on a new machine: install the udev rule once —"
echo "     sudo cp $(basename "$ROOT")/99-hercules-stream.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
echo "   config lives at ~/.config/hercules-stream/config.toml (start once for the hint), logs at ~/.local/state/hercules-stream/"
