"""Single source of truth for repo directory locations.

All scripts live in src/. Resolving everything from this file's location keeps the tools
runnable from any working directory and portable if the repo is ever moved.

Layout (ship vs dev):
  RUNTIME (shipped): src/ (incl. the embedded wake frames, src/wake_data.py), icons/
    (user-selectable display icons), fonts/ (label font), logs/ (run output), config +
    start.sh at root. The daemon runs WITHOUT dev/ — fully self-contained.
  DEV (excluded from shipping): everything under dev/ — captures (dev/pcap; the source
    wake_data.py is generated from via dev/src-re/make_wake.py), docs (dev/docs incl.
    STATE.md/PROTOCOL.md), extracted vendor media, vendor installer binaries, Ghidra
    projects, decompiles, recon, test images.
"""
import os

ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/ -> repo root
SRC         = os.path.join(ROOT, "src")
ICONS       = os.path.join(ROOT, "icons")        # config-selectable icons (see icons/README.md)
FONTS       = os.path.join(ROOT, "fonts")        # Noto Sans (label rendering)
LOGS        = os.path.join(ROOT, "logs")

# Internal, build-baked config overrides (optional). A TOML overlay that OVERRIDES the user's
# config.toml at runtime — see src/features.py. Lives inside the read-only payload (next to the
# code), written by packaging/build-appimage.sh --set; absent in a stock dev checkout.
BUILD_OVERRIDES = os.path.join(SRC, "build_overrides.toml")

# --- XDG homes (packaged runs) -----------------------------------------------------
# A dev checkout keeps config.toml and logs/ in the repo. Packaged runs (AppImage: the
# runtime sets APPDIR and ROOT lives in a read-only squashfs) read the live config from
# ~/.config/hercules-stream/ and write logs to ~/.local/state/hercules-stream/ instead.
APP        = "hercules-stream"
XDG_CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME")
                          or os.path.expanduser("~/.config"), APP)
XDG_STATE  = os.path.join(os.environ.get("XDG_STATE_HOME")
                          or os.path.expanduser("~/.local/state"), APP)
PACKAGED   = bool(os.environ.get("APPDIR")) or not os.access(ROOT, os.W_OK)
if PACKAGED:
    LOGS = XDG_STATE


def config_path():
    """Live config: repo-local config.toml when it exists (dev default), else the XDG
    home (packaged runs; the user copies/edits config.example.toml there)."""
    repo = os.path.join(ROOT, "config.toml")
    return repo if os.path.exists(repo) else os.path.join(XDG_CONFIG, "config.toml")


DEV         = os.path.join(ROOT, "dev")          # RE/development material
PCAP        = os.path.join(DEV, "pcap")          # captures (wake replay reads from here)
MEDIA       = os.path.join(DEV, "media")         # extracted vendor app assets (extract_rcc.py)
RECON       = os.path.join(DEV, "recon")
TEST_IMAGES = os.path.join(DEV, "test-images")
