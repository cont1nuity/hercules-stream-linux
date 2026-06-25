#!/usr/bin/env python3
"""
stream100.py — Linux userspace driver/RE tool for the Hercules Stream 100 (06f8:e053).

The Stream 100 is a vendor-specific USB control surface (4 encoders w/ push, 4 action
buttons, 2 pages, a 4.3" LCD). It is NOT HID. No kernel driver binds it, so libusb/pyusb
can claim it directly.

Subcommands:
  info    Print the USB descriptor / endpoint map.
  probe   Read the input endpoint and hexdump events (byte-diff by default). Use this to
          reverse-engineer the input report, then fill the [protocol] map in your config.
  list    List current PipeWire/PulseAudio sink-inputs (to find config 'match' strings).
  run     Decode input per [protocol] and drive per-app volume/mute via pactl.

Deps: pyusb + libusb-1.0 only (no system packages needed; works inside a venv).
See README.md for SteamOS / udev access setup.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ROOT

VID = 0x06F8
PID = 0xE053
EP_IN = 0x81      # interface 0, interrupt IN  — input events
EP_OUT = 0x02     # interface 0, bulk OUT      — commands (phase 2)
IFACE = 0
REPORT_MAX = 64

# --- Input report layout (reverse-engineered; see CLAUDE.md "Input report") -
FRAME_ID = 0x0C          # byte 0 of every input report
BTN_BYTE = 1             # bitmask: bit0..3 = encoder pushes, bit4..7 = action buttons
ENC_LO = (3, 5, 7, 9)    # int16-LE *absolute* position per encoder (CW=+, CCW=-)

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb not installed. Run:  python3 -m venv .venv && . .venv/bin/activate && pip install pyusb")

import usbdev    # usb.core.find wrapper; honors STREAM100_LIBUSB (AppImage bundled libusb)

# tomllib is stdlib on 3.11+; fall back to tomli if present.
try:
    import tomllib as toml
except ImportError:  # pragma: no cover
    try:
        import tomli as toml
    except ImportError:
        toml = None


# --------------------------------------------------------------------------- device

def open_device(claim=True):
    dev = usbdev.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit(f"Stream 100 ({VID:04x}:{PID:04x}) not found. Is it plugged in?")
    try:
        if dev.get_active_configuration() is None:
            dev.set_configuration()
    except usb.core.USBError:
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
    if claim:
        # No kernel driver should be attached (vendor class), but be safe.
        try:
            if dev.is_kernel_driver_active(IFACE):
                dev.detach_kernel_driver(IFACE)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, IFACE)
        except usb.core.USBError as e:
            sys.exit(f"Cannot claim interface (permission?). Install the udev rule or run with sudo.\n  {e}")
    return dev


def cmd_info(_args):
    dev = open_device(claim=False)
    print(f"Hercules Stream 100  {VID:04x}:{PID:04x}")
    print(f"  Manufacturer: {usb.util.get_string(dev, dev.iManufacturer)}")
    print(f"  Product:      {usb.util.get_string(dev, dev.iProduct)}")
    print(f"  Speed/USB:    bcdUSB {dev.bcdUSB:#06x}")
    for cfg in dev:
        for intf in cfg:
            print(f"  Interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class 0x{intf.bInterfaceClass:02x}")
            for ep in intf:
                ttype = usb.util.endpoint_type(ep.bmAttributes)
                tname = {0: "control", 1: "isoc", 2: "bulk", 3: "interrupt"}.get(ttype, "?")
                d = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) else "OUT"
                print(f"      EP 0x{ep.bEndpointAddress:02x} {d:<3} {tname:<9} "
                      f"{ep.wMaxPacketSize} B  interval {ep.bInterval}")


# --------------------------------------------------------------------------- probe (RE)

def cmd_probe(args):
    dev = open_device()
    print("Probe mode. Turn each encoder both ways, press encoders, press the 4 action")
    print("buttons, switch pages. Watch which byte changes for each. Ctrl-C to stop.\n")
    print("idx: " + " ".join(f"{i:02d}" for i in range(REPORT_MAX)))
    prev = None
    try:
        while True:
            try:
                data = dev.read(EP_IN, REPORT_MAX, timeout=1000)
            except usb.core.USBError as e:
                if e.errno == 110:   # timeout, no event
                    continue
                raise
            buf = bytes(data)
            if args.all or prev is None or buf != prev:
                ts = time.strftime("%H:%M:%S")
                line = " ".join(f"{b:02x}" for b in buf)
                if prev is not None and not args.all:
                    diff = ", ".join(
                        f"[{i}] {prev[i]:02x}->{buf[i]:02x}"
                        for i in range(min(len(prev), len(buf))) if prev[i] != buf[i]
                    )
                    print(f"{ts}  {line}")
                    if diff:
                        print(f"          changed: {diff}")
                else:
                    print(f"{ts}  {line}")
                prev = buf
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        usb.util.release_interface(dev, IFACE)


# --------------------------------------------------------------------------- pactl backend

def _pactl(*args_):
    return subprocess.run(["pactl", *args_], capture_output=True, text=True).stdout


def client_names():
    """Parse `pactl list clients` into {client index: (application.name, process.binary)}."""
    out, cur, names = _pactl("list", "clients"), None, {}
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("Client #"):
            cur = int(line.split("#")[1])
            names[cur] = ["", ""]
        elif cur is None:
            continue
        elif line.startswith("application.name ="):
            names[cur][0] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("application.process.binary ="):
            names[cur][1] = line.split("=", 1)[1].strip().strip('"')
    return names


def sink_inputs():
    """Parse `pactl list sink-inputs` into [{index, mute, vol, app, binary, media, client}].

    Some apps (e.g. Spotify via its own PipeWire loop) attach NO application.* properties to
    the stream itself — only `media.name = "audio-src"` — and the human-readable name lives on
    the owning CLIENT object (pavucontrol shows "spotify: audio-src" by joining the two). For
    such anonymous streams the client's name/binary is filled in, so matching works."""
    out, cur, items = _pactl("list", "sink-inputs"), None, []
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            cur = {"index": int(line.split("#")[1]), "mute": False, "vol": None,
                   "app": "", "binary": "", "media": "", "client": None, "corked": False}
            items.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Client:"):
            tail = line.split(":", 1)[1].strip()
            cur["client"] = int(tail) if tail.isdigit() else None
        elif line.startswith("Mute:"):
            cur["mute"] = line.endswith("yes")
        elif line.startswith("Corked:"):
            cur["corked"] = line.endswith("yes")     # paused stream — silent by definition
        elif line.startswith("Volume:"):
            for tok in line.replace("/", " ").split():
                if tok.endswith("%") and tok[:-1].isdigit():
                    cur["vol"] = int(tok[:-1])
                    break
        elif line.startswith("application.name ="):
            cur["app"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("application.process.binary ="):
            cur["binary"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("media.name ="):
            cur["media"] = line.split("=", 1)[1].strip().strip('"')
    if any(not si["app"] and not si["binary"] and si["client"] is not None for si in items):
        names = client_names()
        for si in items:
            if not si["app"] and not si["binary"] and si["client"] in names:
                si["app"], si["binary"] = names[si["client"]]
    return items


# Generic lane ALIASES: expand to curated alternatives, matched on app/binary ONLY (media
# names carry song/page titles — short tokens like "opera"/"edge" must not match those).
MATCH_ALIASES = {
    "browser": "firefox|waterfox|librewolf|floorp|zen|chrom|brave|opera|vivaldi|edge|falkon|epiphany",
    "game": "wine|proton|.exe|steam_app|gamescope",   # wine/proton incl. Battle.net; Windows exes
}


def match_inputs(needle):
    """Substring match. `needle` may give ALTERNATIVES separated by '|' (e.g.
    "firefox|waterfox") and/or generic ALIASES ("browser", "game" — see MATCH_ALIASES).
    Plain needles match app/binary/media; alias expansions match app/binary only."""
    full, appbin = [], []
    for part in needle.split("|"):
        p = part.strip().lower()
        if not p:
            continue
        if p in MATCH_ALIASES:
            appbin += MATCH_ALIASES[p].lower().split("|")
        else:
            full.append(p)
    out = []
    for si in sink_inputs():
        a, b, m = si["app"].lower(), si["binary"].lower(), si["media"].lower()
        if (any(n in a or n in b or n in m for n in full)
                or any(n in a or n in b for n in appbin)):
            out.append(si)
    return out


def mic_match(lane):
    """Mic-lane syntax: "mic" (or "default mic") = the default input source; "mic:<needle>"
    = a capture source matched by name/description. Returns None if `lane` is not a mic lane,
    "" for the default source, else the needle."""
    s = lane.lower().strip()
    if s in ("mic", "default mic", "mic:default", "mic:"):
        return ""
    if s.startswith("mic:"):
        return lane.split(":", 1)[1].strip()
    return None


def sources():
    """Parse `pactl list sources` into [{index, name, desc, mute, vol, monitor}]. Monitor
    sources (sink loopbacks) are flagged so mic matching can skip them."""
    out, cur, items = _pactl("list", "sources"), None, []
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("Source #"):
            cur = {"index": int(line.split("#")[1]), "name": "", "desc": "",
                   "mute": False, "vol": None, "monitor": False}
            items.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()
            if cur["name"].endswith(".monitor"):
                cur["monitor"] = True
        elif line.startswith("Description:"):
            cur["desc"] = line.split(":", 1)[1].strip()
        elif line.startswith("Monitor of Sink:"):
            if line.split(":", 1)[1].strip() not in ("n/a", ""):
                cur["monitor"] = True
        elif line.startswith("Mute:"):
            cur["mute"] = line.endswith("yes")
        elif line.startswith("Volume:"):
            for tok in line.replace("/", " ").split():
                if tok.endswith("%") and tok[:-1].isdigit():
                    cur["vol"] = int(tok[:-1])
                    break
    return items


def match_sources(needle):
    """Substring match against source name/description; '|' separates alternatives."""
    ns = [n.strip().lower() for n in needle.split("|") if n.strip()]
    return [s for s in sources() if not s["monitor"]
            and any(n in s["name"].lower() or n in s["desc"].lower() for n in ns)]


def vol_change(match, step_pct, verbose):
    mic = mic_match(match)
    if mic is not None:
        if mic == "":
            _pactl("set-source-volume", "@DEFAULT_SOURCE@", f"{step_pct:+d}%")
            if verbose:
                print(f"  default source {step_pct:+d}%")
        else:
            for s in match_sources(mic):
                _pactl("set-source-volume", str(s["index"]), f"{step_pct:+d}%")
                if verbose:
                    print(f"  {s['desc'] or s['name']} #{s['index']} {step_pct:+d}%")
        return
    if match in ("default", "master"):
        _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{step_pct:+d}%")
        if verbose:
            print(f"  default sink {step_pct:+d}%")
        return
    hits = match_inputs(match)
    if not hits and verbose:
        print(f"  (no sink-input matches '{match}')")
    for si in hits:
        _pactl("set-sink-input-volume", str(si["index"]), f"{step_pct:+d}%")
        if verbose:
            print(f"  {si['app'] or si['binary'] or si['media']} #{si['index']} {step_pct:+d}%")


def mute_toggle(match, verbose):
    mic = mic_match(match)
    if mic is not None:
        if mic == "":
            _pactl("set-source-mute", "@DEFAULT_SOURCE@", "toggle")
            if verbose:
                print("  default source mute toggle")
        else:
            for s in match_sources(mic):
                _pactl("set-source-mute", str(s["index"]), "toggle")
                if verbose:
                    print(f"  {s['desc'] or s['name']} #{s['index']} mute toggle")
        return
    if match in ("default", "master"):
        _pactl("set-sink-mute", "@DEFAULT_SINK@", "toggle")
        if verbose:
            print("  default sink mute toggle")
        return
    for si in match_inputs(match):
        _pactl("set-sink-input-mute", str(si["index"]), "toggle")
        if verbose:
            print(f"  {si['app'] or si['binary'] or si['media']} #{si['index']} mute toggle")


def cmd_list(_args):
    for si in sink_inputs():
        print(f"#{si['index']:<4} mute={'Y' if si['mute'] else 'n'}  "
              f"app={si['app']!r} bin={si['binary']!r} media={si['media']!r}")


# --------------------------------------------------------------------------- run

def _s16(v):
    return v - 0x10000 if v >= 0x8000 else v


def enc_pos(buf, k):
    lo = ENC_LO[k]
    return _s16(buf[lo] | (buf[lo + 1] << 8))


def decode_events(buf, state):
    """Decode one input report into normalized events, updating `state`.

    Encoders report an *absolute* signed-16 counter; we emit one step per
    `state['cps']` counts of movement. Buttons fire on the press (0->1) edge.
    Events: ("encoder", k, +/-1) | ("encoder_btn", k) | ("action_btn", k)
    """
    ev = []
    mask = buf[BTN_BYTE]
    for bit in range(8):
        if (mask >> bit) & 1 and not (state["mask"] >> bit) & 1:
            ev.append(("encoder_btn", bit) if bit < 4 else ("action_btn", bit - 4))
    state["mask"] = mask
    for k in range(4):
        cur = enc_pos(buf, k)
        if state["pos"][k] is None:           # first frame: set baseline, don't act
            state["pos"][k] = cur
            continue
        d = _s16((cur - state["pos"][k]) & 0xFFFF)   # wrap-safe delta
        state["pos"][k] = cur
        if d:
            state["acc"][k] += d
            while abs(state["acc"][k]) >= state["cps"]:
                sign = 1 if state["acc"][k] > 0 else -1
                state["acc"][k] -= sign * state["cps"]
                ev.append(("encoder", k, sign))
    return ev


def cmd_run(args):
    if toml is None:
        sys.exit("No TOML parser (need Python 3.11+ or `pip install tomli`).")
    with open(args.config, "rb") as f:
        cfg = toml.load(f)
    settings = cfg.get("settings", {})
    step = int(settings.get("volume_step", 1))             # % per detent
    cps = max(1, int(settings.get("counts_per_step", 6)))  # encoder counts per volume step
    verbose = bool(settings.get("verbose", True))
    pages = cfg.get("pages", [])
    if not pages:
        sys.exit("No [[pages]] defined in config.")

    dev = open_device()
    state = {"mask": 0, "pos": [None] * 4, "acc": [0] * 4, "cps": cps}
    page = 0
    print(f"Running. {len(pages)} page(s), {step}% per {cps} counts. Ctrl-C to stop.")
    try:
        while True:
            try:
                buf = bytes(dev.read(EP_IN, REPORT_MAX, timeout=1000))
            except usb.core.USBError as e:
                if e.errno == 110:        # idle timeout
                    continue
                raise
            if not buf or buf[0] != FRAME_ID:
                continue
            pg = pages[page]
            lanes = pg.get("lanes", [])
            btns = pg.get("buttons", [])
            for ev in decode_events(buf, state):
                if ev[0] == "encoder" and ev[1] < len(lanes):
                    vol_change(lanes[ev[1]], step * ev[2], verbose)
                elif ev[0] == "encoder_btn" and ev[1] < len(lanes):
                    mute_toggle(lanes[ev[1]], verbose)   # push knob = mute its lane
                elif ev[0] == "action_btn" and ev[1] < len(btns):
                    newpage = do_button(btns[ev[1]], verbose, len(pages), page)
                    if newpage is not None:
                        page = newpage
                        if verbose:
                            print(f"page -> {pages[page].get('name', page)}")
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        usb.util.release_interface(dev, IFACE)


def do_button(action, verbose, npages, cur):
    """Run an action-button action. Returns a new page index, or None."""
    if not action or action == "none":
        return None
    scheme, _, arg = action.partition(":")
    if scheme == "mute":
        mute_toggle(arg, verbose)
    elif scheme == "cmd":
        if verbose:
            print(f"  cmd: {arg}")
        subprocess.Popen(arg, shell=True)
    elif scheme == "page":
        if arg == "next":
            return (cur + 1) % npages
        if arg == "prev":
            return (cur - 1) % npages
        if arg.isdigit():
            return int(arg) % npages
    else:
        print(f"  unknown button action: {action!r}")
    return None


# --------------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description="Hercules Stream 100 Linux tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(func=cmd_info)
    pp = sub.add_parser("probe")
    pp.add_argument("--all", action="store_true", help="print every packet, not just changes")
    pp.set_defaults(func=cmd_probe)
    sub.add_parser("list").set_defaults(func=cmd_list)
    pr = sub.add_parser("run")
    pr.add_argument("-c", "--config", default=os.path.join(ROOT, "config.toml"))
    pr.set_defaults(func=cmd_run)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
