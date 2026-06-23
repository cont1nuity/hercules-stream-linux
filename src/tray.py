"""System-tray icon for the Hercules Stream 100 daemon.

Implements org.kde.StatusNotifierItem + com.canonical.dbusmenu directly over D-Bus
(dbus-next, pure python) — no Qt/GTK, which keeps the AppImage small and works on any
StatusNotifierItem host (KDE, GNOME w/ extension, ...). Spawned by ui.py as a SEPARATE
process: the daemon's 20 ms slot loop must never host an event loop. The tray dies with
the daemon (PDEATHSIG + pid poll) and exits silently when there is no session bus,
watcher, or dbus_next — the daemon runs fine trayless.

Left click  -> open the graphical config editor (configui.py; falls back to the raw file).
Right click -> menu: version, Configure… / Edit config file, 'Start at login' checkbox
(an XDG autostart entry, see autostart_* below; DEFAULT ON — created on first tray run
unless the user has unchecked it before), 'Check for updates…' (AppImage runs only — hands off
to AppImageUpdate if present, else opens the Releases page), Restart (relaunch the daemon, e.g.
to apply config edits), Quit (SIGTERM to the daemon).

Usage (by ui.py): tray.py --pid <daemon> --config <path> --version <v> --icon <png/svg>
"""
import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ROOT, XDG_CONFIG, XDG_STATE, LOGS

APP_ID = "hercules-stream"
APP_NAME = "Hercules Stream"
RELEASES_URL = "https://github.com/cont1nuity/hercules-stream-linux/releases/latest"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

# menu item ids
MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART, MI_AUTOUPD, MI_SEP, MI_UPDATE, MI_RESTART, MI_QUIT = \
    1, 2, 3, 4, 5, 6, 7, 8, 9


def die_with_parent():
    """PR_SET_PDEATHSIG — the kernel SIGTERMs us if the daemon dies for ANY reason."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


def opt(a, name, default=""):
    if name in a:
        i = a.index(name)
        v = a[i + 1]
        del a[i:i + 2]
        return v
    return default


# --------------------------------------------------------------------- autostart entry

AUTOSTART_ENTRY = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "autostart", APP_ID + ".desktop")
# set when the user UNchecks the box — without it, every daemon start would re-enable
AUTOSTART_OPTOUT = os.path.join(XDG_STATE, "autostart-disabled")


def launch_cmd():
    """What a login should start: the AppImage that is running us (the runtime exports
    $APPIMAGE = the original .AppImage path) or the repo checkout's start.sh."""
    return os.environ.get("APPIMAGE") or os.path.join(ROOT, "start.sh")


def autostart_enabled():
    return os.path.exists(AUTOSTART_ENTRY)


def autostart_enable():
    cmd = launch_cmd()
    if " " in cmd:
        cmd = '"%s"' % cmd
    os.makedirs(os.path.dirname(AUTOSTART_ENTRY), exist_ok=True)
    with open(AUTOSTART_ENTRY, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=%s\n"
                "Comment=LCD pages + knobs/buttons -> PipeWire\n"
                "Exec=%s\nIcon=%s\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                % (APP_NAME, cmd, APP_ID))
    try:
        os.unlink(AUTOSTART_OPTOUT)
    except FileNotFoundError:
        pass


def autostart_disable():
    try:
        os.unlink(AUTOSTART_ENTRY)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(AUTOSTART_OPTOUT), exist_ok=True)
    open(AUTOSTART_OPTOUT, "w").close()


def autostart_default():
    """Default ON: (re)write the entry on every start so Exec tracks the launch method
    actually in use (AppImage path vs repo start.sh); respect a recorded opt-out."""
    if not os.path.exists(AUTOSTART_OPTOUT):
        autostart_enable()


# --------------------------------------------------------------------------- the icon

def _load_icon_image(path):
    """icon file -> PIL RGBA Image (rsvg-convert for .svg), or None if unavailable."""
    if not path or not os.path.exists(path):
        return None
    try:
        from PIL import Image
        if path.lower().endswith(".svg"):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
                tmp = t.name
            subprocess.run(["rsvg-convert", "-w", "32", "-h", "32", path, "-o", tmp],
                           check=True, capture_output=True)
            im = Image.open(tmp).convert("RGBA")
            os.unlink(tmp)
        else:
            im = Image.open(path).convert("RGBA")
        return im
    except Exception:
        return None


def _greyed(im):
    """Desaturated + dimmed copy of the icon — the 'idle / no device' look. Alpha preserved
    (dimming the RGB only, never the alpha, or the icon would just go transparent)."""
    if im is None:
        return None
    try:
        from PIL import Image, ImageEnhance
        r, g, b, a = im.split()
        rgb = ImageEnhance.Brightness(
            ImageEnhance.Color(Image.merge("RGB", (r, g, b))).enhance(0.0)).enhance(0.55)
        return Image.merge("RGBA", (*rgb.split(), a))
    except Exception:
        return im


def icon_pixmap(im):
    """PIL RGBA Image -> SNI a(iiay) pixmap (ARGB32, network byte order). [] if None."""
    if im is None:
        return []
    raw = im.tobytes()
    argb = bytearray()
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i:i + 4]
        argb += bytes((a, r, g, b))
    return [[im.width, im.height, bytes(argb)]]


def read_state(path):
    """Daemon-published state ('idle'/'active') for the icon; None if absent/unreadable."""
    if not path:
        return None
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ----------------------------------------------------------------- D-Bus service objects

try:
    from dbus_next import Variant, PropertyAccess                  # noqa: E402
    from dbus_next.aio import MessageBus                           # noqa: E402
    from dbus_next.service import (ServiceInterface, method,       # noqa: E402
                                   dbus_property, signal as dbus_signal)
except ImportError as _e:                     # tray is optional — daemon runs without it
    print("tray unavailable: %s (pip/pacman: dbus-next)" % _e, file=sys.stderr)
    sys.exit(0)


class Sni(ServiceInterface):
    def __init__(self, normal_px, greyed_px, tooltip_text):
        super().__init__("org.kde.StatusNotifierItem")
        self._normal = normal_px
        self._greyed = greyed_px
        self._idle = False               # True when the daemon has no device -> greyed icon
        self._tip = tooltip_text
        self.on_activate = lambda: None

    def set_idle(self, idle):
        """Switch between the normal and greyed icon and tell the host to re-read it."""
        if bool(idle) != self._idle:
            self._idle = bool(idle)
            self.NewIcon()
            self.NewToolTip()

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return APP_ID

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return APP_NAME

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return self._greyed if (self._idle and self._greyed) else self._normal

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        tip = "No device connected — idle" if self._idle else self._tip
        return ["", [], APP_NAME, tip]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False                          # left click = Activate (config quick-open)

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def Activate(self, x: "i", y: "i"):
        self.on_activate()

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        self.on_activate()

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass                                  # the host renders /MenuBar itself

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @dbus_signal()
    def NewIcon(self):
        pass

    @dbus_signal()
    def NewToolTip(self):
        pass

    @dbus_signal()
    def NewStatus(self) -> "s":
        return "Active"


def read_check_updates(cfgpath):
    """[ui] check_updates (default True). Best-effort — any parse problem falls back to True."""
    if not cfgpath:
        return True
    try:
        import tomllib as _t
    except ModuleNotFoundError:
        try:
            import tomli as _t
        except ModuleNotFoundError:
            return True
    try:
        with open(cfgpath, "rb") as fh:
            return bool(_t.load(fh).get("ui", {}).get("check_updates", True))
    except Exception:
        return True


def write_check_updates(cfgpath, value):
    """Flip [ui] check_updates in place, touching only that one line so comments/layout survive.
    Creates the key — and the [ui] table — if absent. (The config editor's full comment-preserving
    writer lives in configui.py, which pulls in Tk; the tray must stay Tk-free, hence this.)"""
    if not cfgpath:
        return
    val = "true" if value else "false"
    try:
        with open(cfgpath) as fh:
            lines = fh.read().split("\n")
    except Exception:
        return
    out, in_ui, done = [], False, False
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_ui and not done:                  # leaving [ui] without the key -> insert it here
                out.append("check_updates  = %s" % val)
                done = True
            in_ui = (s == "[ui]")
        elif in_ui and not done and re.match(r"check_updates\s*=", s):
            indent = ln[:len(ln) - len(ln.lstrip())]
            cmt = re.search(r"\s#.*$", ln)          # keep any trailing comment
            out.append("%scheck_updates = %s%s" % (indent, val, cmt.group(0) if cmt else ""))
            done = True
            continue
        out.append(ln)
    if not done:
        if not in_ui:
            out.append("[ui]")
        out.append("check_updates  = %s" % val)
    try:
        with open(cfgpath, "w") as fh:
            fh.write("\n".join(out))
    except Exception:
        pass


def _ver_tuple(s):
    """'v1.2.0' / '1.2.0' -> (1, 2, 0); None for non-release strings like 'dev' (never nagged)."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def latest_release_version():
    """Latest release tag WITHOUT the GitHub API (no token, no rate limit): a HEAD on
    /releases/latest 302-redirects to /releases/tag/vX.Y.Z — read the tag off the final URL."""
    req = urllib.request.Request(RELEASES_URL, method="HEAD", headers={"User-Agent": APP_ID})
    with urllib.request.urlopen(req, timeout=6) as resp:
        final = resp.geturl()
    m = re.search(r"/tag/(v?[0-9][0-9.]*)", final)
    return m.group(1) if m else None


def notify(summary, body):
    """Desktop notification via libnotify if present; skipped silently otherwise (the tray menu
    still shows the update either way)."""
    ns = shutil.which("notify-send")
    if not ns:
        return
    try:
        subprocess.Popen([ns, "-a", APP_NAME, summary, body], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _selftest():
    """Offline checks for the version compare + the one-key config writer (no network/Tk/dbus)."""
    assert _ver_tuple("v1.2.0") == (1, 2, 0)
    assert _ver_tuple("1.10.3") == (1, 10, 3)
    assert _ver_tuple("dev") is None
    assert _ver_tuple("1.2.0") < _ver_tuple("1.2.1")
    p = tempfile.mktemp(suffix=".toml")
    with open(p, "w") as f:
        f.write("[ui]\nbrightness = 80  # keep me\ncheck_updates = true\n")
    write_check_updates(p, False)
    txt = open(p).read()
    assert "check_updates = false" in txt and "# keep me" in txt, txt
    assert read_check_updates(p) is False
    p2 = tempfile.mktemp(suffix=".toml")
    with open(p2, "w") as f:
        f.write("# header\n[[pages]]\nname = 'x'\n")
    write_check_updates(p2, True)
    assert "[ui]" in open(p2).read() and read_check_updates(p2) is True
    print("tray selftest ok")


class Menu(ServiceInterface):
    """com.canonical.dbusmenu menu ('Check for updates…' appears only for AppImage runs)."""

    def __init__(self, cfgpath, daemon_pid, quit_ev):
        super().__init__("com.canonical.dbusmenu")
        self.revision = 1
        self.cfgpath = cfgpath
        self.daemon_pid = daemon_pid
        self.quit_ev = quit_ev
        self._cfgui = None                    # the running config-editor process, if any
        self.update_available = None          # latest version string, once a newer release is seen
        self.check_updates = read_check_updates(cfgpath)

    # ---- menu model ----

    def _props(self, mid):
        home = os.path.expanduser("~")
        shown = self.cfgpath.replace(home, "~", 1) if self.cfgpath.startswith(home) else self.cfgpath
        if mid == MI_INFO:
            return {"label": Variant("s", "%s — version %s" % (APP_NAME, VERSION)),
                    "enabled": Variant("b", False)}
        if mid == MI_CONFIG:
            return {"label": Variant("s", "Configure…")}
        if mid == MI_EDIT:
            return {"label": Variant("s", "Edit config file: %s" % shown)}
        if mid == MI_AUTOSTART:
            return {"label": Variant("s", "Start at login"),
                    "toggle-type": Variant("s", "checkmark"),
                    "toggle-state": Variant("i", 1 if autostart_enabled() else 0)}
        if mid == MI_SEP:
            return {"type": Variant("s", "separator")}
        if mid == MI_AUTOUPD:
            return {"label": Variant("s", "Check for updates automatically"),
                    "toggle-type": Variant("s", "checkmark"),
                    "toggle-state": Variant("i", 1 if self.check_updates else 0)}
        if mid == MI_UPDATE:
            lbl = ("Update available: %s — install now" % self.update_available
                   if self.update_available else "Check for updates…")
            return {"label": Variant("s", lbl)}
        if mid == MI_RESTART:
            return {"label": Variant("s", "Restart %s" % APP_NAME)}
        if mid == MI_QUIT:
            return {"label": Variant("s", "Quit %s" % APP_NAME)}
        return {"children-display": Variant("s", "submenu")}        # root

    def _layout(self):
        appimage = bool(os.environ.get("APPIMAGE"))   # update items only make sense for the AppImage
        ids = [MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART]
        if appimage:
            ids.append(MI_AUTOUPD)
        ids.append(MI_SEP)
        if appimage:
            ids.append(MI_UPDATE)
        ids += [MI_RESTART, MI_QUIT]
        kids = [Variant("(ia{sv}av)", [mid, self._props(mid), []]) for mid in ids]
        return [0, self._props(0), kids]

    # ---- actions ----

    def open_config(self):
        try:
            subprocess.Popen(["xdg-open", self.cfgpath], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def open_config_ui(self):
        """Launch the Tkinter config editor (configui.py) under the same interpreter that
        runs us. Single-instance (don't stack windows). Falls back to opening the raw file
        when Tk/PIL aren't importable — e.g. an AppImage that doesn't bundle Tcl/Tk yet."""
        if self._cfgui is not None and self._cfgui.poll() is None:
            return                                  # editor already open
        import importlib.util
        gui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configui.py")
        try:
            have = all(importlib.util.find_spec(m) for m in ("tkinter", "PIL"))
        except Exception:
            have = False
        if os.path.exists(gui) and have:
            try:
                os.makedirs(LOGS, exist_ok=True)
                err = open(os.path.join(LOGS, "configui.err"), "wb")   # why the editor didn't open
                self._cfgui = subprocess.Popen([sys.executable, gui, "--config", self.cfgpath],
                                               start_new_session=True, stdout=err, stderr=err)
                return
            except Exception:
                pass
        self.open_config()                          # fallback: xdg-open the raw TOML

    def _clicked(self, mid):
        if mid == MI_CONFIG:
            self.open_config_ui()
        elif mid == MI_EDIT:
            self.open_config()
        elif mid == MI_AUTOSTART:
            (autostart_disable if autostart_enabled() else autostart_enable)()
            self.revision += 1
            self.ItemsPropertiesUpdated()
            self.LayoutUpdated()
        elif mid == MI_AUTOUPD:
            self.check_updates = not self.check_updates
            write_check_updates(self.cfgpath, self.check_updates)
            self._refresh()
        elif mid == MI_UPDATE:
            self._check_updates()
        elif mid == MI_RESTART:
            self._restart()
        elif mid == MI_QUIT:
            try:
                os.kill(self.daemon_pid, signal.SIGTERM)
            except Exception:
                pass
            self.quit_ev.set()

    def _check_updates(self):
        """AppImage only: hand off to AppImageUpdate for an in-place delta update if the tool is
        installed, else open the Releases page. The AppImage carries embedded update-information
        (baked by packaging/build-appimage.sh), so the updater needs nothing but the AppImage
        path."""
        appimage = os.environ.get("APPIMAGE")
        tool = shutil.which("appimageupdatetool") or shutil.which("AppImageUpdate")
        target = [tool, appimage] if (appimage and tool) else ["xdg-open", RELEASES_URL]
        try:
            subprocess.Popen(target, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _refresh(self):
        """Tell the host our menu changed (bump revision + re-emit), so labels/checkmarks repaint."""
        self.revision += 1
        self.ItemsPropertiesUpdated()
        self.LayoutUpdated()

    def set_update_available(self, version):
        """Called by the startup check when a newer release exists: relabel the update item."""
        self.update_available = version
        self._refresh()

    def _restart(self):
        """Relaunch the daemon. single_instance() takes the flock NON-blocking, so a new
        daemon started while the old one still holds it would just bail — therefore spawn
        a DETACHED relauncher (start_new_session, so it outlives our own PDEATHSIG when the
        daemon dies) that waits for the old pid to exit (releasing the flock) and only then
        execs the launch command; the fresh daemon spawns its own tray. SIGTERM the daemon
        (its clean-shutdown path) and quit ourselves only AFTER the relauncher is up."""
        cmd = launch_cmd()
        # wait for the daemon pid to vanish (bounded ~15 s so a hung pid can't spin us
        # forever), then exec the AppImage / start.sh. paths go via env to survive spaces.
        waiter = ('i=0; while kill -0 "$DPID" 2>/dev/null && [ "$i" -lt 75 ]; do '
                  'sleep 0.2; i=$((i+1)); done; exec "$CMD"')
        try:
            subprocess.Popen(["/bin/sh", "-c", waiter], start_new_session=True,
                             env={**os.environ, "DPID": str(self.daemon_pid), "CMD": cmd},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return                            # couldn't relaunch -> leave the daemon running
        try:
            os.kill(self.daemon_pid, signal.SIGTERM)
        except Exception:
            pass
        self.quit_ev.set()

    # ---- com.canonical.dbusmenu ----

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @method()
    def GetLayout(self, parent: "i", depth: "i", names: "as") -> "u(ia{sv}av)":
        return [self.revision, self._layout()]

    @method()
    def GetGroupProperties(self, ids: "ai", names: "as") -> "a(ia{sv})":
        ids = ids or [0, MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART, MI_AUTOUPD, MI_SEP,
                      MI_UPDATE, MI_RESTART, MI_QUIT]
        return [[i, self._props(i)] for i in ids]

    @method()
    def GetProperty(self, mid: "i", name: "s") -> "v":
        return self._props(mid).get(name, Variant("s", ""))

    @method()
    def Event(self, mid: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self._clicked(mid)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for mid, event_id, _data, _ts in events:
            if event_id == "clicked":
                self._clicked(mid)
        return []

    @method()
    def AboutToShow(self, mid: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @dbus_signal()
    def ItemsPropertiesUpdated(self) -> "a(ia{sv})a(ias)":
        return [[[m, self._props(m)] for m in (MI_AUTOSTART, MI_AUTOUPD, MI_UPDATE)], []]

    @dbus_signal()
    def LayoutUpdated(self) -> "ui":
        return [self.revision, 0]


# --------------------------------------------------------------------------------- main

VERSION = "dev"


async def _check_for_update(menu):
    """One startup check, off the dbus loop: if the latest GitHub release is newer than the running
    version, relabel the tray item and fire a desktop notification. Best-effort — network/SSL
    failures just no-op (the manual 'Check for updates…' item still works)."""
    try:
        latest = await asyncio.to_thread(latest_release_version)
    except Exception:
        return
    cur, new = _ver_tuple(VERSION), _ver_tuple(latest)
    if new and cur and new > cur:
        menu.set_update_available(latest)
        notify(APP_NAME, "Update available: %s — open the tray to install" % latest)


async def amain(daemon_pid, cfgpath, icon, state_file):
    bus = await MessageBus().connect()
    quit_ev = asyncio.Event()
    menu = Menu(cfgpath, daemon_pid, quit_ev)
    im = _load_icon_image(icon)
    sni = Sni(icon_pixmap(im), icon_pixmap(_greyed(im)),
              "version %s — config: %s" % (VERSION, cfgpath))
    last_state = read_state(state_file)       # correct icon from the first paint (no flicker)
    sni._idle = (last_state == "idle")
    sni.on_activate = menu.open_config_ui
    bus.export(MENU_PATH, menu)
    bus.export(ITEM_PATH, sni)
    intr = await bus.introspect(WATCHER, "/StatusNotifierWatcher")
    watcher = bus.get_proxy_object(WATCHER, "/StatusNotifierWatcher", intr) \
                 .get_interface("org.kde.StatusNotifierWatcher")
    await watcher.call_register_status_notifier_item(bus.unique_name)
    if os.environ.get("APPIMAGE") and menu.check_updates:   # AppImage-only, honors the toggle
        asyncio.create_task(_check_for_update(menu))
    while not quit_ev.is_set():
        try:
            os.kill(daemon_pid, 0)            # daemon gone -> tray goes too
        except OSError:
            break
        st = read_state(state_file)           # daemon publishes idle/active at session boundaries
        if st and st != last_state:
            last_state = st
            sni.set_idle(st == "idle")        # greyed when no device is attached
        try:
            await asyncio.wait_for(quit_ev.wait(), 1.0)
        except asyncio.TimeoutError:
            pass


def main():
    global VERSION
    if "--selftest" in sys.argv[1:]:
        _selftest()
        return
    die_with_parent()
    a = sys.argv[1:]
    daemon_pid = int(opt(a, "--pid", "0") or "0")
    cfgpath = opt(a, "--config")
    VERSION = opt(a, "--version", "dev") or "dev"
    icon = opt(a, "--icon")
    state_file = opt(a, "--state-file")
    if not daemon_pid:
        sys.exit("usage: tray.py --pid <daemon> [--config p] [--version v] [--icon p] "
                 "[--state-file p]")
    autostart_default()
    asyncio.run(amain(daemon_pid, cfgpath, icon, state_file))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:                    # no bus / no watcher / no dbus_next at import
        print("tray unavailable: %s" % e, file=sys.stderr)
        sys.exit(0)
