#!/usr/bin/env python3
"""stream200.py — Linux userspace driver for the Hercules Stream 200 XLR (06f8:e054, and the
e055 sibling). Sister module to stream100.py; it shares the device-agnostic PipeWire backend
(volume/mute matching) but speaks a COMPLETELY different USB protocol.

⚠ EXPERIMENTAL / bring-up. The transport and the telemetry FRAME LAYOUT are decoded and
wire-confirmed offline against the Windows USB capture (dev/docs/STREAM200-XLR-*.md), but no
build has talked to a real 200 XLR yet, and the exact bit/offset of each physical control is
not hardware-verified.

STATUS: intended work-in-progress — KEEP. This module (and src/sm200.py) is deliberately
retained for hardware bring-up; do NOT remove it as "dead" or "speculative" code.

Control model — the 200 XLR's MAIN area is identical to the Stream 100: 4 dials (each with
push-to-mute) + 4 action buttons, configured per page via [[pages]] EXACTLY like the 100. The
RIGHT side adds 5 buttons (creator/audience/link/mute/next-page) + 1 headphone dial; those are
configured in the small [stream200] section (a button -> an action, the headset -> an audio lane).

Per the project rule (CLAUDE.md "Don't guess protocol bytes") the telemetry bit/offset of each
physical control is NOT user config and is NOT guessed: it lives in the BUTTON_BITS / DIAL_FIELDS
tables below (one place, code), currently all None = unknown. Fill them once from a real device:

    PYTHONPATH=src python3 dev/src-re/stream200_probe.py input   # prints (byte,bit)/(offset,size)

Wire facts used here (all wire-confirmed 2026-06-15, NO byte is invented):
  topology   : vendor interface IF3 — bulk OUT 0x01 (commands) / bulk IN 0x81 (telemetry)
  frame      : [len:u32 LE][0x01][opcode][seq:u8][0x00] + payload      (NO CRC anywhere)
  op-0x01    : 12-byte GET-STATE poll ; reply = 104-byte telemetry on IN 0x81
  transport  : bulk OUT + a ZLP when len % wMaxPacketSize == 0 ; bulk IN = ack/telemetry
  seq        : rolling u8 1..255, skips ONLY 0x00 (0xFF is valid) — capture-confirmed
               (the vendor app's poll stream shows 254->255->1; an earlier decompile note
               claimed "skips 0x00/0xFF" but the wire disproves it — captures win)

Display: the op-0x07/op-0x08 image *payload* format is not decoded, so this module never
synthesises pixels (observe-and-copy only — see stream200_probe.py `display`). The audio half
(knobs/buttons -> PipeWire) works once BUTTON_BITS/DIAL_FIELDS are filled; the panel stays as
the firmware left it until the payload codec is reverse-engineered on hardware.

Subcommands:
  info      Dump the IF3 descriptors and a few telemetry polls (read-only).
  run       Poll telemetry and drive PipeWire (4 lanes via [[pages]] + [stream200] extras).
  selftest  OFFLINE (no device): frame builder + control-map + decoder + daemon checks.
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import config_path, ROOT
import stream100 as S            # device-agnostic PipeWire backend (vol/mute/match) + tomllib
from sm200 import next_seq       # shared rolling seq — defined once, in the wire-frame builder

VID = 0x06F8
PIDS = (0xE054, 0xE055)          # e054 on the wire; e055 = undocumented sibling (HSM02)
IFACE = 3                        # vendor interface (bulk OUT 0x01 / bulk IN 0x81)
EP_OUT = 0x01
EP_IN = 0x81
TELEM_LEN = 104                  # bulk-IN telemetry frame length

# Telemetry regions (decoded). The EXACT bit/offset of each physical control is a HARDWARE fact
# (see BUTTON_BITS / DIAL_FIELDS below), never user config.
BTN_LO, BTN_HI = 12, 15          # button bitmask bytes [12,13,14] -> a 24-bit field
DIAL_LO, DIAL_HI = 16, 84        # f32 dial fields (incl. dial3 @ off80 = bytes 80-83)
VU_LO, VU_HI = 84, 92            # per-channel level/VU telemetry (int16)
HEADSET_OFF = 92                 # bounded headset pot, absolute u8 0..100
SEQ_OFF = 6                      # rolling seq in both command and telemetry frames

# --- physical control -> telemetry location (THE HARDWARE MAP) ------------------------------
# The 200 XLR's main area is identical to the Stream 100: 4 dials (each with push-to-mute) + 4
# action buttons, configured per page via [[pages]] EXACTLY like the 100. The right side adds 5
# buttons + 1 headphone dial, configured in [stream200]. These tables say WHICH telemetry bit /
# offset each physical control is. Per CLAUDE.md we never guess these bytes and they live here in
# code (one place), never in user config; confirm/extend on a real device with
# `dev/src-re/stream200_probe.py input` (it prints ready-to-paste entries).
#
# Values below were recovered by TIME-CORRELATING the two USB input streams in the full capture
# (dev/pcap/Hercules-200-XLR-Export.pcapng): the labeled UAC2 control-change events (intr 0x82,
# CN/CS + timestamps) against the vendor-telemetry bit/float changes (bulk IN 0x81). Confidence
# tags: CONFIRMED (strong time-correlation), LIKELY (value solid, label/order a best guess),
# None (UNKNOWN — not exercised in the capture; pin with the probe on hardware).
#
# BUTTON_BITS: name -> (byte, bit). The capture is BYTE-ALIGNED, one control type per byte
# (recovered 2026-06-19 from the full pcap's labeled press order; tools dev/pcap/xlr_button_edges.py
# + xlr_byte_changes.py, ~1 s over the 955 MB capture):
#   * byte-14 bits 0..3 = the 4 dial-PUSH mutes (1st press group, t=16.7/17.9/19.2/21.2 s) —
#     bits CONFIRMED vs UAC2 MUTE CN 7/5/3/9; wired left-to-right (push1=b14.0 .. push4=b14.3) to
#     match act1..4 (per-lane CN = 7/5/3/9 in that press order; exact lane<->CN pairing still LIKELY).
#   * byte-13 bits 0..3 = the 4 ACTION buttons under the dials (2nd press group,
#     t=32.7/33.9/35.3/37.1 s; tester pressed them left-to-right -> act1=b13.0 .. act4=b13.3).
#   * byte-12 bits 0..3 = the 4 MAIN DIALS turning (t=48.9..56.0 s; "Drehknöpfe v.l.n.r.") —
#     NOT buttons; the dial values come from DIAL_FIELDS, so these bits are not wired here.
# STILL None: the 5 RIGHT-SIDE deck buttons (All-mute/Link/Page/Creator/Audience). They leave
# almost no telemetry edge in this capture — only a lone byte-12 bit-4 transient (t=39.7 s) sits
# in the deck window — so they may not be host-readable via the poll frame; pin on hardware with
# dev/src-re/stream200_probe.py input. (Earlier the page button was guessed as byte-13 bit-0; that
# bit is act1, so the guess is dropped.)
BUTTON_BITS = {                  # control name -> (byte, bit)
    "push1": (14, 0), "push2": (14, 1), "push3": (14, 2), "push4": (14, 3),  # dial-push mutes (L->R press order)
    "act1": (13, 0), "act2": (13, 1), "act3": (13, 2), "act4": (13, 3),  # action buttons (L->R): 2nd press group
    "creator": None, "audience": None, "link": None,            # right-side deck: no clear telemetry edge here
    "mute": None, "page": None,                                 # (only a lone b12.4 sits in the deck window)
}
# DIAL_FIELDS: name -> (offset, kind). The 4 MAIN dials are endless rotary encoders (like the
# Stream 100's), reported as little-endian FLOAT32 (clamped value ~0..10000) → driven RELATIVELY
# (delta). The HEADSET dial is a bounded ~270° pot → reported as an absolute u8 0..100 (offset 92)
# → driven ABSOLUTELY (knob position = volume); see ABSOLUTE_DIALS. Recovered by sweeping each dial
# full-CW then full-CCW in the capture and time-correlating (one dial per window). RE-VERIFIED
# 2026-06-18 against the full 174 s capture (7233 telemetry frames, 194 UAC2 events): an
# exhaustive byte-by-byte (aligned + unaligned) scan over offsets 16..103 found EXACTLY three
# clean, low-reversal (revfrac ~0.00-0.04), temporally NON-OVERLAPPING f32 sweep windows in the
# dial region, plus one bounded u8 sweep — one control per window, sequential as the owner swept:
#   off 80 (f32) t=68..77s,  CONFIRMED dial (NOT VU): single CCW→hold@7→CW gesture, 233 pts,
#                1 reversal. Companion bytes 84/88/92 quiet in this window. No CN emitted.
#   off 16 (f32) t=93..104s, CONFIRMED dial: clean up-then-down sweep 0..10000, 4 reversals.
#                off 48 == off16 EXACTLY (true mirror, 0 diffs) — do NOT wire it. No CN emitted.
#   off 28 (f32) t=107..116s, CONFIRMED dial = UAC2 CN1/CN2 (the ONLY dial that emitted VOLUME
#                events: 162 CS=2 events on CN1&CN2 in 107.7..115.6s, exact window overlap).
#                off 60 == off28 - 1600 (constant-bias companion VIEW of the same dial) — do NOT wire.
#   off 92 (u8)  t=158..167s, CONFIRMED headset pot: bounded 0..100, hold@100 → 100→0 → hold@0 →
#                0→100 → hold@100 (52 runs, 1 reversal) = the ~270° absolute pot. No CN emitted.
# Only 4 of the 5 knobs were swept in this capture → the 4th MAIN lane dial is genuinely ABSENT
# from the telemetry (no sweep window anywhere in 16..103) → stays None (probe it on hardware).
# ⚠ which f32 offset is which physical lane is still LIKELY (only off28↔CN1/CN2 is anchored).
# CORRECTIONS to earlier passes (do not regress):
#   * 60/64/68/72/76 are NOT dials — they only stepped once in the 0.4s init burst (stored defaults).
#   * The earlier "two more dials at off 80 and off 92" claim was HALF right: off 80 IS a dial
#     (3rd main), off 92 IS the headset — neither is VU. The actual VU/level telemetry lives in
#     bytes 84..91 (signed int16 level fields, e.g. value at 88-89, nonzero in all 7233 frames,
#     ramping/oscillating in audio-correlated windows 16-55s/91-116s/118-139s) — NOT dial sweeps.
DIAL_FIELDS = {                  # control name -> (offset, kind in _DIAL_SIZE)
    "dial1":   (28, "f32"),   # CONFIRMED offset (turned dial = UAC2 CN1/CN2, exact window); lane label LIKELY
    "dial2":   (16, "f32"),   # CONFIRMED dial (clean 0..10000 sweep 93-104s; off48 == off16 mirror, not wired); no CN
    "dial3":   (80, "f32"),   # CONFIRMED dial (clean CCW/CW sweep 68-77s; NOT VU); no CN
    "dial4":   None,          # UNKNOWN — 4th main dial genuinely ABSENT from this capture (no sweep window)
    "headset": (92, "u8"),    # CONFIRMED — bounded ~270° pot, absolute 0..100, sweep 158-167s (see ABSOLUTE_DIALS)
}
_DIAL_SIZE = {"u8": 1, "u16": 2, "u32": 4, "f32": 4}   # bytes per dial-field kind
# Bounded-pot dials whose value IS the volume (absolute): emit a ("dial_abs", name, percent) set
# instead of relative steps. The 4 endless main encoders are NOT here (they drive relative deltas).
ABSOLUTE_DIALS = {"headset"}
DIAL_STEP_DEFAULT = 100.0        # f32 units of (relative) dial movement per volume step; the f32
                                 # dials run ~0..10000, so ~100/step ≈ 100 steps end-to-end (HW-tune)
N_LANES = 4                      # main lanes — same as the Stream 100
LANE_DIALS = ["dial1", "dial2", "dial3", "dial4"]
LANE_PUSHES = ["push1", "push2", "push3", "push4"]
ACTION_BTNS = ["act1", "act2", "act3", "act4"]
GLOBAL_BTNS = ["creator", "audience", "link", "mute", "page"]   # right-side, config-actioned


def _mapped(table):
    """(name, loc) for every control with a known location (skips the None placeholders)."""
    return [(n, loc) for n, loc in table.items() if loc is not None]


def _dial_value(frame, off, kind):
    """Read a dial field as its numeric value (float32 or unsigned int)."""
    raw = frame[off:off + _DIAL_SIZE[kind]]
    return struct.unpack("<f", raw)[0] if kind == "f32" else int.from_bytes(raw, "little")


# --------------------------------------------------------------------------- frame builder
def build_op1(seq):
    """op-0x01 GET-STATE poll: [0c 00 00 00][01][01][seq][00] + 4 zero payload bytes."""
    body = bytes([0x01, 0x01, seq & 0xFF, 0x00]) + b"\x00\x00\x00\x00"
    return struct.pack("<I", 4 + len(body)) + body


def valid_telemetry(f):
    """A 104-byte telemetry reply with the expected constant header (len/marker/opcode-echo)."""
    return (f is not None and len(f) >= TELEM_LEN
            and f[4] == 0x01 and f[5] == 0x81)


def decode_regions(f):
    """Structured view of a telemetry frame — raw region bytes, no control interpretation."""
    if not valid_telemetry(f):
        return {"short": f.hex() if f else None}
    return {"seq": f[SEQ_OFF],
            "buttons": f[BTN_LO:BTN_HI].hex(),
            "dials": f[DIAL_LO:DIAL_HI].hex(),
            "vu": f[VU_LO:VU_HI].hex(),
            "headset": f[HEADSET_OFF]}


# --------------------------------------------------------------------------- control map
class ControlMap:
    """The [stream200] intent: what each RIGHT-SIDE addition does. The 4 main lanes + 4 action
    buttons are NOT here — they come from [[pages]], exactly like the Stream 100. Buttons map a
    global-button name -> action (mute:/cmd:/page:/none); the headset dial maps -> an audio lane.
    Byte/bit/offset are NOT here either — those are the hardware map (BUTTON_BITS/DIAL_FIELDS)."""

    def __init__(self, actions, headset_lane, headset_inv):
        self.actions = actions              # global button name -> action string
        self.headset_lane = headset_lane
        self.headset_inv = headset_inv

    @classmethod
    def from_config(cls, cfg):
        s = cfg.get("stream200", {}) or {}
        actions = {n: "none" for n in GLOBAL_BTNS}
        actions["page"] = "page:next"       # the next-page button switches pages by default
        for b in s.get("buttons", []) or []:
            n = b.get("name")
            if n in GLOBAL_BTNS:
                actions[n] = b.get("action") or "none"
            elif n:
                print("  [stream200] ignoring unknown button %r (expected one of: %s)"
                      % (n, ", ".join(GLOBAL_BTNS)))
        headset_lane, headset_inv = "", False
        for d in s.get("dials", []) or []:
            if d.get("name") == "headset":
                headset_lane = d.get("lane", "") or ""
                headset_inv = bool(d.get("invert", False))
            elif d.get("name"):
                print("  [stream200] ignoring unknown dial %r (only \"headset\" is global; the "
                      "4 lane dials are configured per page)" % d.get("name"))
        return cls(actions, headset_lane, headset_inv)


def _signed_delta(cur, prev, size):
    """Wrap-safe signed difference of two unsigned `size`-byte counters."""
    mod = 1 << (size * 8)
    d = (cur - prev) % mod
    return d - mod if d >= mod // 2 else d


def new_state(cps=6, dial_step=DIAL_STEP_DEFAULT):
    """`cps` = integer counts per step (u8/u16/u32 dial fields); `dial_step` = float-units per
    step (f32 dial fields). The 200's dials are f32 (capture-derived) so `dial_step` is what
    matters; both are calibratable on hardware."""
    return {"btn_mask": None, "dial_prev": {}, "dial_acc": {},
            "cps": max(1, int(cps)), "dial_step": float(dial_step) or 1.0}


def decode_events(frame, state):
    """Decode one telemetry frame into raw control events using the HARDWARE map (BUTTON_BITS /
    DIAL_FIELDS); only mapped (non-None) controls fire. Events:
        ("button",   name, "press"|"release")   — name is a BUTTON_BITS key
        ("dial",     name, +/-steps)             — endless encoder, RELATIVE movement
        ("dial_abs", name, percent)              — bounded pot (name in ABSOLUTE_DIALS), 0..100
    A relative dial emits one step per `state["cps"]` of integer movement (u* fields) or per
    `state["dial_step"]` of float movement (f32 fields). An absolute dial emits its 0..100
    position whenever it changes. The daemon interprets each name (lane push / action button /
    global button / lane dial / headset). The first frame only sets the baseline (no events)."""
    ev = []
    mask = frame[BTN_LO] | (frame[BTN_LO + 1] << 8) | (frame[BTN_LO + 2] << 16)
    if state["btn_mask"] is None:
        state["btn_mask"] = mask                   # baseline; don't act on the resting state
    else:
        changed = mask ^ state["btn_mask"]
        for name, (byte, bit) in _mapped(BUTTON_BITS):
            pos = (byte - BTN_LO) * 8 + bit
            if changed & (1 << pos):
                ev.append(("button", name, "press" if (mask & (1 << pos)) else "release"))
        state["btn_mask"] = mask

    for name, (off, kind) in _mapped(DIAL_FIELDS):
        val = _dial_value(frame, off, kind)
        prev = state["dial_prev"].get(name)
        state["dial_prev"][name] = val
        if prev is None:
            continue
        if name in ABSOLUTE_DIALS:                 # bounded pot: position IS the volume %
            pct = max(0, min(100, int(round(val))))
            if pct != max(0, min(100, int(round(prev)))):
                ev.append(("dial_abs", name, pct))
            continue
        if kind == "f32":
            delta, unit = val - prev, state["dial_step"]
        else:
            delta, unit = _signed_delta(val, prev, _DIAL_SIZE[kind]), state["cps"]
        if not delta:
            continue
        acc = state["dial_acc"].get(name, 0.0) + delta
        steps = int(acc / unit)                    # trunc toward 0; works for either sign
        if steps:
            acc -= steps * unit
            ev.append(("dial", name, steps))
        state["dial_acc"][name] = acc
    return ev


# --------------------------------------------------------------------------- transport
def _is_access_error(e):
    if getattr(e, "errno", None) in (1, 13):       # EPERM, EACCES
        return True
    s = str(e).lower()
    return "access" in s or "permission" in s or "denied" in s


class Transport:
    """Bulk vendor-interface transport for the 200 XLR. Open claims IF3 only (never the UAC2
    audio interfaces — the device stays a normal sound card). `write` appends a ZLP on a
    wMaxPacketSize boundary; `poll` sends op-0x01 and reads the 104-byte telemetry reply."""

    def __init__(self):
        import usb.core
        import usb.util
        import usbdev
        self._core = usb.core
        self._util = usb.util
        self._find = usbdev.find
        self.dev = None
        self.pid = None
        self.maxout = 512

    def _open_handle(self):
        for pid in PIDS:
            d = self._find(idVendor=VID, idProduct=pid)
            if d is not None:
                self.pid = pid
                return d
        return None

    def open(self):
        dev = self._open_handle()
        if dev is None:
            raise SystemExit("Stream 200 XLR (06f8:e054/e055) not found — is it plugged in?")
        try:
            if dev.is_kernel_driver_active(IFACE):
                dev.detach_kernel_driver(IFACE)
        except (NotImplementedError, self._core.USBError):
            pass
        last = None
        for attempt in range(4):
            try:
                self._util.claim_interface(dev, IFACE)
                last = None
                break
            except self._core.USBError as e:
                last = e
                if _is_access_error(e):
                    break
                if attempt == 1:
                    try:
                        dev.reset()
                    except Exception:
                        pass
                time.sleep(0.4)
        if last is not None:
            if _is_access_error(last):
                raise SystemExit(
                    "no access to the Stream 200 XLR vendor interface (permissions).\n"
                    "  install the udev rule (covers e053/e054/e055) and replug:\n"
                    "    sudo cp %s /etc/udev/rules.d/ && sudo udevadm control --reload-rules"
                    " && sudo udevadm trigger" % os.path.join(ROOT, "99-hercules-stream.rules"))
            raise SystemExit("IF%d busy — another process holds it." % IFACE)
        self.dev = dev
        try:                                       # resolve OUT maxpacket for the ZLP rule
            for intf in dev.get_active_configuration():
                if intf.bInterfaceNumber == IFACE:
                    for ep in intf:
                        if ep.bEndpointAddress == EP_OUT:
                            self.maxout = int(ep.wMaxPacketSize)
        except Exception:
            pass
        return self.pid

    def write(self, buf):
        self.dev.write(EP_OUT, buf, timeout=800)
        if self.maxout and len(buf) % self.maxout == 0:
            try:
                self.dev.write(EP_OUT, b"", timeout=300)     # ZLP — end-of-transfer
            except Exception:
                pass

    def read_in(self, n=TELEM_LEN, timeout=600):
        try:
            return bytes(self.dev.read(EP_IN, n, timeout=timeout))
        except self._core.USBError:
            return None

    def poll(self, seq):
        self.write(build_op1(seq))
        return self.read_in(TELEM_LEN)

    def close(self):
        try:
            if self.dev is not None:
                self._util.release_interface(self.dev, IFACE)
                self._util.dispose_resources(self.dev)
        except Exception:
            pass


# --------------------------------------------------------------------------- daemon
class _Daemon:
    """Drives PipeWire from decoded telemetry events. The 4 main lanes + 4 action buttons behave
    EXACTLY like the Stream 100 (read from [[pages]]); the right-side additions use [stream200].
    All pactl work is offloaded to the AudioWorker so the poll loop never blocks."""

    def __init__(self, cfg, worker, verbose):
        self.pages = cfg.get("pages", []) or []
        self.step = int(cfg.get("settings", {}).get("volume_step", 1))
        self.cm = ControlMap.from_config(cfg)
        self.worker = worker
        self.verbose = verbose
        self.page = 0

    def _lanes(self):
        if not self.pages:
            return [""] * N_LANES
        return (self.pages[self.page].get("lanes", []) + [""] * N_LANES)[:N_LANES]

    def _page_buttons(self):
        if not self.pages:
            return [""] * N_LANES
        return (self.pages[self.page].get("buttons", []) + [""] * N_LANES)[:N_LANES]

    def _action(self, action):
        """Run a page/global action string (mute:/cmd:/page:/none) — same scheme as the 100."""
        if not action or action == "none":
            return
        scheme, _, arg = action.partition(":")
        if scheme == "page":
            n = len(self.pages) or 1
            if arg == "next":
                self.page = (self.page + 1) % n
            elif arg == "prev":
                self.page = (self.page - 1) % n
            elif arg.isdigit():
                self.page = int(arg) % n
            if self.verbose:
                print("page -> %s" % (self.pages[self.page].get("name", self.page)
                                      if self.pages else self.page))
        elif scheme == "mute":
            self.worker.q.put(("mute", arg))
        elif scheme == "cmd":
            self.worker.q.put(("button", action))
        elif self.verbose:
            print("  unknown action %r" % action)

    def handle(self, ev):
        kind, name, val = ev
        if kind == "button":
            if val != "press":
                return                          # act on the press edge only
            if name in LANE_PUSHES:             # dial push -> mute that lane (like the 100)
                lane = self._lanes()[LANE_PUSHES.index(name)]
                if lane:
                    self.worker.q.put(("mute", lane))
            elif name in ACTION_BTNS:           # action button -> this page's button action
                self._action(self._page_buttons()[ACTION_BTNS.index(name)])
            elif name in self.cm.actions:       # right-side global button -> its [stream200] action
                self._action(self.cm.actions[name])
        elif kind == "dial":                    # endless encoder -> RELATIVE volume change
            if name in LANE_DIALS:              # main lane dial -> that page-lane's volume
                lane = self._lanes()[LANE_DIALS.index(name)]
                if lane:
                    self.worker.q.put(("vol", lane, val * self.step))
        elif kind == "dial_abs":                # bounded pot -> ABSOLUTE volume (% = position)
            if name == "headset" and self.cm.headset_lane:
                pct = (100 - val) if self.cm.headset_inv else val
                self.worker.q.put(("setvol", self.cm.headset_lane, pct))


def run_daemon(cfg, cfgpath=None, debug=False):
    """The Stream 200 XLR daemon: poll telemetry → drive PipeWire. The 4 main lanes/buttons
    behave exactly like the Stream 100 (pages); the right-side buttons + headset dial come from
    [stream200]. The poll loop never blocks on pactl (offloaded to the AudioWorker). The panel
    display is not driven yet (image-payload codec pending hardware)."""
    import signal
    from ui import AudioWorker, Dbg            # lazy: device-agnostic, avoids import cycles

    settings = cfg.get("settings", {})
    verbose = bool(settings.get("verbose", True))
    s200 = cfg.get("stream200", {}) or {}
    cps = s200.get("counts_per_step", settings.get("counts_per_step", 6))
    dial_step = float(s200.get("dial_step", DIAL_STEP_DEFAULT))   # f32 units/step (calibrate HW)
    poll_hz = float(s200.get("poll_hz", 50))
    period = 1.0 / max(1.0, poll_hz)

    dbg = Dbg(debug)
    tr = Transport()
    pid = tr.open()
    worker = AudioWorker(verbose, dbg)
    worker.start()                              # meter_factory stays None: no panel VU on 200
    dmn = _Daemon(cfg, worker, verbose)

    def _term(_s, _f):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    n_btn, n_dial = len(_mapped(BUTTON_BITS)), len(_mapped(DIAL_FIELDS))
    print("Stream 200 XLR (06f8:%04x) daemon up: %d page(s); hardware map: %d/%d buttons, "
          "%d/%d dials." % (pid, len(dmn.pages), n_btn, len(BUTTON_BITS),
                            n_dial, len(DIAL_FIELDS)))
    if n_dial:
        print("  dials: capture-derived float fields (offsets confirmed; which knob is which + "
              "dial_step are PROVISIONAL — verify on hardware).")
    if n_btn == 0:
        print("  buttons: UNMAPPED — their telemetry bits must be filled in "
              "BUTTON_BITS (src/stream200.py) from a real device before they work.")
    print("  panel display: not yet available (image-payload codec pending hardware). "
          "Ctrl-C to stop.")
    dbg.log("STREAM200 daemon start pid=%04x pages=%d mapped_btn=%d mapped_dial=%d poll_hz=%.0f",
            pid, len(dmn.pages), n_btn, n_dial, poll_hz)

    state = new_state(cps, dial_step)
    seq = 1
    next_t = time.monotonic()
    last_mon = next_t
    n_poll = n_telem = n_ev = 0
    try:
        while True:
            frame = tr.poll(seq)
            seq = next_seq(seq)
            n_poll += 1
            if valid_telemetry(frame):
                n_telem += 1
                for ev in decode_events(frame, state):
                    n_ev += 1
                    dbg("EVENT %s", ev)
                    dmn.handle(ev)
            while True:                          # drain worker readbacks (no display: discard)
                try:
                    worker.results.get_nowait()
                except Exception:
                    break
            now = time.monotonic()
            if verbose and now - last_mon > 10.0:
                print("[mon] polls=%d telemetry=%d events=%d page=%d worker_q=%d"
                      % (n_poll, n_telem, n_ev, dmn.page, worker.q.qsize()))
                dbg.log("STATS polls=%d telemetry=%d events=%d", n_poll, n_telem, n_ev)
                last_mon = now
            next_t += period                     # absolute pacing -> no cumulative drift
            dt = next_t - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            elif dt < -0.5:
                next_t = time.monotonic()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        dbg.log("STREAM200 daemon stop polls=%d telemetry=%d events=%d", n_poll, n_telem, n_ev)
        tr.close()


# --------------------------------------------------------------------------- cli modes
def cmd_info(_args):
    tr = Transport()
    pid = tr.open()
    print("Hercules Stream 200 XLR  %04x:%04x" % (VID, pid))
    cfg = tr.dev.get_active_configuration()
    for intf in cfg:
        print("  IF%d alt%d class=0x%02x" % (intf.bInterfaceNumber, intf.bAlternateSetting,
                                             intf.bInterfaceClass))
        for ep in intf:
            print("    ep 0x%02x %-3s maxpkt=%d" % (
                ep.bEndpointAddress, "IN" if ep.bEndpointAddress & 0x80 else "OUT",
                ep.wMaxPacketSize))
    print("  telemetry polls:")
    seq = 1
    for _ in range(5):
        f = tr.poll(seq)
        seq = next_seq(seq)
        print("    %s" % decode_regions(f))
        time.sleep(0.05)
    tr.close()


def cmd_run(args):
    if S.toml is None:
        sys.exit("No TOML parser (need Python 3.11+ or `pip install tomli`).")
    with open(args.config, "rb") as f:
        cfg = S.toml.load(f)
    run_daemon(cfg, cfgpath=args.config, debug=args.debug)


def selftest(_args=None):
    """OFFLINE (no device): frame builder, control-map parsing, the telemetry decoder, and the
    daemon's event routing — incl. that the 4 main lanes behave exactly like the Stream 100."""
    ok = True

    f1 = build_op1(0xC7)
    if f1.hex() != "0c0000000101c70000000000":
        print("  build_op1 FAIL: %s" % f1.hex()); ok = False
    else:
        print("  build_op1 byte-shape: OK (%s)" % f1.hex())

    if [next_seq(x) for x in (0xFE, 0xFF, 0x00)] != [0xFF, 0x01, 0x01]:
        print("  next_seq wrap FAIL"); ok = False
    else:
        print("  next_seq 0xFE->0xFF, 0xFF->0x01, 0x00->0x01 (skip only 0x00): OK")

    # ControlMap: only the right-side additions (global button actions + headset lane); the
    # 4 main lanes/buttons come from [[pages]]. Unknown names are ignored.
    cfg = {"settings": {"volume_step": 1},
           "pages": [{"name": "P1", "lanes": ["default", "mic", "discord", "game"],
                      "buttons": ["mute:mic", "none", "none", "page:next"]},
                     {"name": "P2", "lanes": ["a", "b", "c", "d"],
                      "buttons": ["none", "none", "none", "page:prev"]}],
           "stream200": {"buttons": [{"name": "creator", "action": "cmd:true"},
                                     {"name": "mute", "action": "mute:default"},
                                     {"name": "bogus", "action": "x"}],          # ignored
                         "dials": [{"name": "headset", "lane": "default"},
                                   {"name": "line9", "lane": "x"}]}}             # ignored
    cm = ControlMap.from_config(cfg)
    if (cm.actions.get("creator") == "cmd:true" and cm.actions.get("mute") == "mute:default"
            and cm.actions.get("page") == "page:next" and cm.headset_lane == "default"
            and "bogus" not in cm.actions):
        print("  control map: global actions + headset lane parsed, unknown ignored: OK")
    else:
        print("  control map FAIL: %s headset=%r" % (cm.actions, cm.headset_lane)); ok = False

    # Shipped (pcap-derived) map: 3 endless f32 dials + the u8 headset pot; mutes (byte14) and
    # the 4 action buttons (byte13 bits0-3) wired from the capture's labeled press order.
    sd = {n: v for n, v in DIAL_FIELDS.items() if v is not None}
    sb = {n: v for n, v in BUTTON_BITS.items() if v is not None}
    pushes = ("push1", "push2", "push3", "push4")
    acts = ("act1", "act2", "act3", "act4")
    f32off = sorted(v[0] for v in sd.values() if v[1] == "f32")
    if (f32off == [16, 28, 80] and sd.get("headset") == (92, "u8")
            and "headset" in ABSOLUTE_DIALS
            and all(sb.get(p) == (14, i) for i, p in enumerate(pushes))
            and all(sb.get(a) == (13, i) for i, a in enumerate(acts))):
        print("  shipped map: f32 dials @16/28/80 + u8 headset@92(abs), push mutes byte14 (L->R), "
              "action buttons byte13 bits0-3 (L->R): OK")
    else:
        print("  shipped map FAIL: dials=%s buttons=%s" % (sd, sb)); ok = False

    # Decoder + daemon: install a clean test hardware map (one f32 endless dial + the u8 headset
    # pot), synthesize frames/events.
    saved_b, saved_d = dict(BUTTON_BITS), dict(DIAL_FIELDS)
    BUTTON_BITS.clear(); BUTTON_BITS.update({"creator": (13, 1), "push1": (12, 0)})
    DIAL_FIELDS.clear(); DIAL_FIELDS.update({"dial1": (16, "f32"), "headset": (20, "u8")})
    try:
        def frame(btn=0, d1=0.0, hs=0):
            f = bytearray(TELEM_LEN)
            f[0:8] = bytes([0x68, 0, 0, 0, 0x01, 0x81, 0x01, 0x00])
            f[12], f[13] = btn & 0xFF, (btn >> 8) & 0xFF
            f[16:20] = struct.pack("<f", d1)
            f[20] = hs & 0xFF                         # headset: absolute u8 0..100
            return bytes(f)

        st = new_state(cps=4, dial_step=100.0)
        creator = 1 << ((13 - BTN_LO) * 8 + 1)       # byte13 bit1
        decode_events(frame(), st)                   # baseline (no events)
        e1 = decode_events(frame(btn=creator), st)   # creator press
        e2 = decode_events(frame(btn=0), st)         # creator release
        e3 = decode_events(frame(d1=250.0), st)      # endless dial +250.0 @ step 100 -> +2 steps
        e4 = decode_events(frame(d1=250.0, hs=50), st)  # headset pot 0 -> 50 (absolute)
        press = ("button", "creator", "press") in e1
        rel = ("button", "creator", "release") in e2
        dials = [e for e in e3 if e[0] == "dial" and e[1] == "dial1"]
        habs = [e for e in e4 if e[0] == "dial_abs" and e[1] == "headset"]
        if (press and rel and dials == [("dial", "dial1", 2)]
                and habs == [("dial_abs", "headset", 50)]):
            print("  decoder: button edges + f32 relative steps + headset absolute %: OK")
        else:
            print("  decoder FAIL: press=%s rel=%s dials=%s habs=%s"
                  % (press, rel, dials, habs)); ok = False

        class _Q:
            def __init__(s): s.items = []
            def put(s, x): s.items.append(x)

        class _W:
            def __init__(s): s.q = _Q()

        w = _W()
        dmn = _Daemon(cfg, w, verbose=False)
        dmn.handle(("dial", "dial1", 3))             # page0 lane0 "default" volume +3 (relative)
        dmn.handle(("button", "push1", "press"))     # dial push -> mute page0 lane0 "default"
        dmn.handle(("button", "creator", "press"))   # right-side -> cmd:true
        dmn.handle(("button", "mute", "press"))      # right-side -> mute:default
        dmn.handle(("dial_abs", "headset", 50))      # bounded pot -> setvol "default" 50 (absolute)
        routed = w.q.items == [("vol", "default", 3), ("mute", "default"),
                               ("button", "cmd:true"), ("mute", "default"),
                               ("setvol", "default", 50)]
        dmn.handle(("button", "act4", "press"))      # page0 button[3] = page:next -> page 1
        p1 = dmn.page
        dmn.handle(("button", "page", "press"))      # global page btn = page:next -> wraps to 0
        paging = (p1 == 1 and dmn.page == 0)
        if routed and paging:
            print("  daemon: 4 lanes like the 100 + headset absolute + paging works: OK")
        else:
            print("  daemon FAIL: routed=%s items=%s paging=%s" % (routed, w.q.items, paging))
            ok = False
    finally:
        BUTTON_BITS.clear(); BUTTON_BITS.update(saved_b)
        DIAL_FIELDS.clear(); DIAL_FIELDS.update(saved_d)

    if _signed_delta(2, 0xFFFFFFFE, 4) == 4 and _signed_delta(0xFFFFFFFE, 2, 4) == -4:
        print("  wrap-safe counter delta: OK")
    else:
        print("  wrap-safe delta FAIL"); ok = False

    print("stream200 selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="Hercules Stream 200 XLR Linux driver (experimental)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(func=cmd_info)
    pr = sub.add_parser("run")
    pr.add_argument("-c", "--config", default=config_path())
    pr.add_argument("--debug", action="store_true", help="device-level trace -> logs/")
    pr.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=selftest)
    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
