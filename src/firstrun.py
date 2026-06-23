"""First-run / every-start preflight for the daemon (shipping task 4, NATIVE-UI-PLAN.md).

Runs before ui.py touches the device. Instead of dying on a missing config or an
unreadable USB node it explains and fixes interactively:

  - host tools: `pactl`/`parec` (PipeWire/Pulse client tools) must exist — offers to
    install the distro's client-tools package via pkexec (pacman/apt-get/dnf/zypper
    detected), manual hint otherwise.
  - config: offers to create the live config from config.example.toml (repo config.toml on
    dev checkouts, ~/.config/hercules-stream/ on packaged runs — wherever paths.config_path()
    resolved).
  - device: distinguishes "not plugged in" (retry prompt) from "no permission on the USB
    node" (offer to install the udev rule via pkexec — the native polkit password dialog —
    then wait for access; replug prompt when the rule alone doesn't take, e.g. non-logind
    setups where only the plug event applies MODE/GROUP).

Dialog backends, picked at runtime: kdialog or zenity when a display session exists, console
prompts on a TTY, and a non-interactive fallback otherwise (auto-create the config, print
instructions, exit on hard blockers) — so a login-autostart run self-heals what it can and
fails loud on what it can't.

pkexec detail: the helper script and the rule are copied to a private world-readable temp
dir before pkexec runs — root cannot read inside the AppImage's FUSE mount (squashfuse
mounts are owner-only), so the privileged half must never execute from $APPDIR.

This module never builds protocol bytes and never touches audio state: the device probe is
a read-only get_active_configuration(), disposed immediately.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ROOT

RULE_NAME = "99-hercules-stream.rules"
RULE_DST = os.path.join("/etc/udev/rules.d", RULE_NAME)
TITLE = "Hercules Stream"

# Root half of the rule install. Mirrors setup.sh step 3, incl. the SteamOS
# readonly dance; the EXIT trap re-locks the rootfs even when a step fails.
HELPER = """#!/bin/sh
set -e
ro=0
command -v steamos-readonly >/dev/null 2>&1 && ro=1
[ "$ro" = 1 ] && steamos-readonly disable
trap '[ "$ro" = 1 ] && steamos-readonly enable' EXIT
cp "$1" "%(dst)s"
chmod 644 "%(dst)s"
udevadm control --reload-rules
udevadm trigger
""" % {"dst": RULE_DST}


# --------------------------------------------------------------------------- dialogs

_BACKEND = None


def _backend():
    """kdialog/zenity in a graphical session, console prompts on a TTY, else 'none'."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = "none"
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            for cand in ("kdialog", "zenity"):
                if shutil.which(cand):
                    _BACKEND = cand
                    break
        if _BACKEND == "none" and sys.stdin is not None and sys.stdin.isatty():
            _BACKEND = "tty"
    return _BACKEND


def _call(cmd):
    try:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return 1


def _ask(title, text):
    """Yes/no question. Non-interactive runs answer No (callers then exit with a hint)."""
    be = _backend()
    if be == "kdialog":
        return _call(["kdialog", "--title", "%s — %s" % (TITLE, title), "--yesno", text]) == 0
    if be == "zenity":
        return _call(["zenity", "--question", "--title", "%s — %s" % (TITLE, title),
                      "--text", text]) == 0
    print("\n[%s] %s" % (title, text))
    if be == "tty":
        try:
            return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    print("(non-interactive: assuming No)")
    return False


def _info(title, text, error=False):
    be = _backend()
    if be == "kdialog":
        _call(["kdialog", "--title", "%s — %s" % (TITLE, title),
               "--error" if error else "--msgbox", text])
    elif be == "zenity":
        _call(["zenity", "--error" if error else "--info",
               "--title", "%s — %s" % (TITLE, title), "--text", text])
    else:
        print("\n[%s] %s" % (title, text))


# --------------------------------------------------------------------------- checks

def _probe(desc):
    """Probe one supported device descriptor (a devices.Device). True = usable, False =
    present but permission denied, None = not present.

    Read-only: get_active_configuration() forces libusb to open the node, which is exactly the
    call that fails with EACCES when the udev rule is missing."""
    import usb.core
    import usb.util
    import usbdev
    dev = usbdev.find(idVendor=desc.vid, idProduct=desc.pid)
    if dev is None:
        return None
    try:
        dev.get_active_configuration()
        return True
    except usb.core.USBError as e:
        denied = e.errno == 13 or "access" in str(e).lower() or "permission" in str(e).lower()
        return not denied
    except Exception:
        return True                      # odd state — let the daemon's own open report it
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def _scan():
    """Probe every supported device. Returns (usable, denied): the first descriptor that is
    accessible (or None), and the first that is present-but-permission-denied (or None)."""
    import devices
    usable = denied = None
    for desc in devices.SUPPORTED:
        st = _probe(desc)
        if st is True and usable is None:
            usable = desc
        elif st is False and denied is None:
            denied = desc
    return usable, denied


def _wait_access(secs):
    """Poll for any usable supported device (udevadm trigger usually re-grants the uaccess ACL
    without a replug; give it a moment before asking the user to replug). Returns the
    descriptor that became accessible, or None."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        usable, _ = _scan()
        if usable:
            return usable
        time.sleep(0.5)
    return None


def _pkexec(argv, manual):
    """Run argv as root via the polkit password dialog (text agent on a TTY). True on
    success; explains a cancelled/failed auth and prints the manual fallback otherwise."""
    if not shutil.which("pkexec"):
        _info("Setup error",
              "pkexec (polkit) not found — run manually:\n%s" % manual, error=True)
        return False
    rc = subprocess.call(["pkexec"] + list(argv))
    if rc == 0:
        return True
    if rc in (126, 127):                 # dismissed / not authorized
        _info("Authorization", "Authorization was cancelled — nothing was changed.")
    else:
        _info("Setup error", "Failed (exit %d) — run manually:\n%s" % (rc, manual),
              error=True)
    return False


def install_rule_pkexec():
    """Install the udev rule as root via the polkit password dialog. True on success."""
    src = os.path.join(ROOT, RULE_NAME)
    if not os.path.exists(src):
        _info("Setup error", "udev rule %s not found in %s" % (RULE_NAME, ROOT), error=True)
        return False
    manual = ("sudo cp %s /etc/udev/rules.d/ && sudo udevadm control --reload-rules "
              "&& sudo udevadm trigger" % src)
    tmp = tempfile.mkdtemp(prefix="hercules-stream-setup-")
    try:
        os.chmod(tmp, 0o755)
        rule = os.path.join(tmp, RULE_NAME)
        shutil.copyfile(src, rule)
        os.chmod(rule, 0o644)
        helper = os.path.join(tmp, "install-udev-rule.sh")
        with open(helper, "w") as f:
            f.write(HELPER)
        os.chmod(helper, 0o755)
        return _pkexec([helper, rule], manual)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- preflight

# pactl and parec ship in ONE client-tools package everywhere; whichever package manager
# exists picks the distro. SteamOS gets no auto-install attempt — its pacman sits on a
# read-only rootfs with no populated keyring (and SteamOS ships pactl/parec anyway).
PKG_MGRS = (
    ("pacman", ("pacman", "-S", "--noconfirm", "--needed"), "libpulse"),
    ("apt-get", ("apt-get", "install", "-y"), "pulseaudio-utils"),
    ("dnf", ("dnf", "install", "-y"), "pulseaudio-utils"),
    ("zypper", ("zypper", "--non-interactive", "install"), "pulseaudio-utils"),
)


def _check_tools():
    missing = [t for t in ("pactl", "parec") if not shutil.which(t)]
    if not missing:
        return
    mgr = None
    if not shutil.which("steamos-readonly"):
        mgr = next((m for m in PKG_MGRS if shutil.which(m[0])), None)
    manual = ("sudo %s %s" % (" ".join(mgr[1]), mgr[2]) if mgr else
              "install your distro's PipeWire/Pulse client tools "
              "(package: libpulse / pulseaudio-utils)")
    if mgr and _backend() != "none":
        name, cmd, pkg = mgr
        if _ask("Missing audio tools",
                "%s not found — the daemon needs the PipeWire/Pulse client tools.\n\n"
                "Install package '%s' now via %s? (asks for the administrator password)"
                % (" and ".join(missing), pkg, name)):
            if _pkexec(list(cmd) + [pkg], manual):
                missing = [t for t in missing if not shutil.which(t)]
                if not missing:
                    print("installed %s — audio tools available" % pkg)
                    return
    _info("Missing audio tools",
          "%s not found.\n%s, then start again." % (" and ".join(missing), manual),
          error=True)
    sys.exit("missing host tools: %s — %s" % (", ".join(missing), manual))


def _check_config(cfgpath):
    if os.path.exists(cfgpath):
        return
    example = os.path.join(ROOT, "config.example.toml")
    # Non-interactive runs self-heal silently: a default config is harmless and the
    # alternative (exit) would make a fresh login-autostart install fail forever.
    if _backend() != "none":
        if not _ask("Create config?",
                    "No config found at\n%s\n\nCreate it from the bundled default "
                    "(Main page: system volume, mic, Discord, game)?" % cfgpath):
            sys.exit("no config at %s\n  copy %s\n  there and edit lanes/icons "
                     "(icons/README.md)" % (cfgpath, example))
    d = os.path.dirname(cfgpath)
    if d:
        os.makedirs(d, exist_ok=True)
    shutil.copyfile(example, cfgpath)
    print("created default config at %s" % cfgpath)


def _check_device():
    """Return the accessible device descriptor (devices.Device) if one is attached, or None when
    nothing supported is plugged in — the daemon then idles in the tray with all functionality
    off and starts the moment a device is hotplugged (no nagging dialog). sys.exit only on an
    unresolved permission problem. The udev rule covers the whole family (e053/e054/e055), so
    one install path serves the Stream 100 and the Stream 200 XLR."""
    while True:
        usable, denied = _scan()
        if usable:
            return usable
        if denied is None:
            # Nothing supported is plugged in: don't nag. The daemon idles in the tray with all
            # functionality off and brings the device up on hotplug (the supervisor in ui.py).
            return None
        # present but EACCES on the node
        if os.path.exists(RULE_DST):
            if not _ask("No device access",
                        "The udev rule is installed but the %s is not accessible yet.\n"
                        "Unplug and replug it, then choose Yes to retry." % denied.name):
                sys.exit("device present but not accessible — replug it (rule %s is installed)"
                         % RULE_DST)
            _wait_access(5)
            continue
        if not _ask("Device permissions",
                    "The %s was found, but this user may not access it yet.\n\n"
                    "Install the udev permission rule now? (asks for the administrator "
                    "password once)" % denied.name):
            sys.exit("device present but not accessible — install the udev rule:\n"
                     "  sudo cp %s /etc/udev/rules.d/ && sudo udevadm control --reload-rules"
                     " && sudo udevadm trigger\nthen replug the device"
                     % os.path.join(ROOT, RULE_NAME))
        rule_ok = install_rule_pkexec()
        if rule_ok:
            got = _wait_access(8)
            if got:
                return got
        # rule is in place now but the ACL didn't take → loop falls into the replug branch


def preflight(cfgpath):
    """All startup checks. Returns the accessible device descriptor (devices.Device), or None
    when no device is attached (the daemon idles in the tray until one is hotplugged); sys.exits
    on an unresolved tools/config/permission problem. The caller dispatches on `desc.kind` and
    treats None as 'no device yet'."""
    _check_tools()
    _check_config(cfgpath)
    return _check_device()


if __name__ == "__main__":
    from paths import config_path
    desc = preflight(config_path())
    print("preflight: OK (tools, config, device access) — %s" % (desc.name if desc else "?"))
