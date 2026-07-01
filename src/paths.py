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


# --- self-install home (packaged runs) ---------------------------------------------
# A per-app XDG data dir where the daemon parks its OWN AppImage when it had to relocate a
# throwaway copy (a fresh download in ~/Downloads), so the original can be deleted without
# breaking autostart. Nothing else watches a per-app subdir of ~/.local/share (Shelly /
# appimaged scan ~/.local/bin; AppImageLauncher uses ~/Applications), so no tool moves,
# renames, or double-manages it. An AppImage a tool already parked in a real home is adopted
# where it lives instead of being copied here (see install_target / tray.maybe_self_install).
INSTALL_DIR      = os.path.join(os.environ.get("XDG_DATA_HOME")
                                or os.path.expanduser("~/.local/share"), APP)
INSTALL_APPIMAGE = os.path.join(INSTALL_DIR, "Hercules-Stream.AppImage")

EPHEMERAL_DIRS = None   # None -> the default throwaway set below; a list overrides it (tests)


def _ephemeral_dirs():
    home = os.path.expanduser("~")
    return EPHEMERAL_DIRS if EPHEMERAL_DIRS is not None else [
        os.environ.get("XDG_DOWNLOAD_DIR") or os.path.join(home, "Downloads"),
        os.path.join(home, "Desktop"), "/tmp", "/var/tmp", "/media", "/mnt", "/run/media"]


def _is_ephemeral(path):
    """True if `path` is in a throwaway/download location — the AppImage just landed there, so
    it should be relocated. A deliberate home (a tool's install dir, a folder the user chose) is
    False, so it is adopted in place instead of duplicated. ponytail: tune the list above."""
    rp = os.path.realpath(path)
    return any(rp == os.path.realpath(s) or rp.startswith(os.path.realpath(s) + os.sep)
               for s in _ephemeral_dirs())


def install_target():
    """The persistent path a login should launch: our private INSTALL_APPIMAGE when running from
    a throwaway copy (tray.maybe_self_install relocates it there), the current $APPIMAGE when it
    already lives in a real home (adopt in place — don't fight a tool that installed it), else the
    repo start.sh on a dev/source run. tray.launch_cmd() and daemonctl.launch_cmd() both return this."""
    src = os.environ.get("APPIMAGE")
    if not src:
        return os.path.join(ROOT, "start.sh")
    return INSTALL_APPIMAGE if _is_ephemeral(src) else os.path.realpath(src)


DEV         = os.path.join(ROOT, "dev")          # RE/development material
PCAP        = os.path.join(DEV, "pcap")          # captures (wake replay reads from here)
MEDIA       = os.path.join(DEV, "media")         # extracted vendor app assets (extract_rcc.py)
RECON       = os.path.join(DEV, "recon")
TEST_IMAGES = os.path.join(DEV, "test-images")
