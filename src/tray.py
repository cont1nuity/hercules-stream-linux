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
import shutil
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ROOT, XDG_CONFIG, XDG_STATE, LOGS

APP_ID = "hercules-stream"
APP_NAME = "Hercules Stream"
RELEASES_URL = "https://github.com/cont1nuity/hercules-stream-linux/releases/latest"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

# menu item ids
MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART, MI_SEP, MI_UPDATE, MI_RESTART, MI_QUIT = 1, 2, 3, 4, 5, 6, 7, 8


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


class Menu(ServiceInterface):
    """com.canonical.dbusmenu menu ('Check for updates…' appears only for AppImage runs)."""

    def __init__(self, cfgpath, daemon_pid, quit_ev):
        super().__init__("com.canonical.dbusmenu")
        self.revision = 1
        self.cfgpath = cfgpath
        self.daemon_pid = daemon_pid
        self.quit_ev = quit_ev
        self._cfgui = None                    # the running config-editor process, if any

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
        if mid == MI_UPDATE:
            return {"label": Variant("s", "Check for updates…")}
        if mid == MI_RESTART:
            return {"label": Variant("s", "Restart %s" % APP_NAME)}
        if mid == MI_QUIT:
            return {"label": Variant("s", "Quit %s" % APP_NAME)}
        return {"children-display": Variant("s", "submenu")}        # root

    def _layout(self):
        ids = [MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART, MI_SEP]
        if os.environ.get("APPIMAGE"):          # in-place updates only make sense for the AppImage
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
        ids = ids or [0, MI_INFO, MI_CONFIG, MI_EDIT, MI_AUTOSTART, MI_SEP, MI_UPDATE, MI_RESTART, MI_QUIT]
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
        return [[[MI_AUTOSTART, self._props(MI_AUTOSTART)]], []]

    @dbus_signal()
    def LayoutUpdated(self) -> "ui":
        return [self.revision, 0]


# --------------------------------------------------------------------------------- main

VERSION = "dev"


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
