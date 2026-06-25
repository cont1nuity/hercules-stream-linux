#!/usr/bin/env python3
"""Native UI daemon — drives the Stream 100 like the Windows app, on Linux (dev/docs/NATIVE-UI-PLAN.md).

ONE process: encoders/buttons drive PipeWire (stream100.py logic) AND the panel shows a live
native UI — per-lane icon, label, volume readout (op41), mute state, optional VU bars (op40),
page switching. Every wire primitive used here is HW/photo-verified (dev/docs/ELEMENT-LAYER-FINDINGS.md).

Threading model (v2..v5 — each thread exists because its work must not touch the cadence):
  - MAIN thread owns the 20 ms display cadence (display.Scheduler: seq@958 + CRC@956 on every
    frame). Per slot: drain queued input events, then send exactly ONE frame:
    queued burst frame > dirty op41/op30 update > VU frame (every 2nd slot) > heartbeat.
    NOTHING in this loop may block: the panel has no framebuffer and blanks the moment the
    heartbeat cadence stalls.
  - INPUT reader thread (v5) does BLOCKING 1 s reads on EP 0x81 — the exact pattern proven by
    `stream100.py run` — and queues raw reports. v2-v4 polled EP 0x81 from the cadence loop
    with 2-3 ms timeouts = libusb submitting + CANCELLING an interrupt transfer ~50x/s;
    rapid cancel churn is the prime suspect for the input dropouts seen on HW runs 2/3
    (dead at start, dies again after idle). The reader also self-recovers (clear_halt on
    error) and feeds the always-on [mon] status line.
  - AUDIO worker thread owns ALL pactl subprocess calls. The display reacts to a detent
    instantly from local state; the worker applies it to the OS and reads the TRUE value back.

Volume model (v3): the OS is the source of truth. A detent is applied as
read-current -> clamp(current + delta, 0..100) -> set, so the device can never push the OS
past 100% (PipeWire itself allows 150%). The readback and a periodic 2 s re-sync of the
current page keep the on-screen number honest against changes made elsewhere (OS mixer).
Local prediction only bridges the ~100 ms until the worker's readback lands.

VU model (v4): bar k ALWAYS meters lane k's own audio — no separate mapping. App lanes meter
that app's stream (parec --monitor-stream=<sink-input index>); "default" lanes meter
@DEFAULT_MONITOR@. The 2 s sync tracks sink-input indices, so meters follow apps as their
streams come and go (and rebind on page switch). All metering is read-only.

Label rendering (v3): matched against captured op36 labels (app's own Qt rendering:
~10 px glyphs, white core w/ ~12 antialias greys, rows 4..13 of 16 — see recon/labelref/).
Noto Sans SemiBold 14, composited on opaque black (keeps AA greys), luminance boost 1.6
(mimics Qt hinting's solid white core), bbox-centered. v2 hard-thresholded alpha to full
white at y=0 — jagged and misplaced ("barely readable" on glass).

Init: captured wake replay cut at the capture's FIRST element draw (URBs 0..~32 verbatim) —
wakes the render pipeline, never paints the Windows app's icons. First content = our burst.

Observe-and-copy: the page-switch burst mirrors page-change.pcapng f000 (op30 off, op32 page
variant, op34 x8, op41 x8 -> elements -> op30 on, op32 idle variant); op32 bytes stay VERBATIM
capture constants (only the VU-enable bits are decoded); op34 dial slots are intent-built from
config (a/b/c color model HW-verified 2026-06-12), the op34 button bank stays verbatim.

Usage:
  python3 src/ui.py --selftest            # offline: CRC-validate + op_walk every authored frame
  python3 src/ui.py -c config.toml        # hardware run (config: see config.example.toml)
  python3 src/ui.py -c config.toml --debug  # + device-level I/O trace -> logs/ui-debug.log
  python3 src/ui.py --no-preflight        # skip the first-run checks (firstrun.py dialogs)
Audio safety: metering is parec on .monitor sources (read-only); volume/mute changes happen
ONLY on user input events, and never above 100%.
"""
import os
import queue as _queue
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sm
import vu_crc
import op_walk
import codec_element as ce
import stream100 as S
from paths import FONTS, ICONS, MEDIA, ROOT, LOGS, config_path

W_ICON, H_ICON = 32, 32
W_LBL, H_LBL = 110, 16
OPQ = 0xF << 16                  # RLE nib 0xF = opaque (matches captured elements)
LBL_FONT = os.path.join(FONTS, "NotoSans-SemiBold.ttf")
LBL_SIZE = 14                    # SemiBold 14 = 47x10 px "Master" — matches captured label
LBL_BOOST = 1.6                  # white-core ratio 0.69 vs captured 0.60 (Qt hinting)

# --- verbatim baseline bytes (observe-and-copy; semantics of op32/op34 partially open) -------
# op32 channel-config: page-change.pcapng f000/f008 burst variant + f006/f014 idle variant.
OP32_PAGE = bytes.fromhex("0084010f0f6964840107076964840107076964840107076964")
OP32_IDLE = bytes.fromhex("00840107076964840107076964840107076964840107076964")
# op34 per-slot meter config: [34][sel][a:u16][b:u16][c:u16][cnt][cnt x RGB565 LE].
# HW-verified 2026-06-12 (dev/src-re/abc_test.py): colors[] = the bar BODY, cnt>=2 = a
# firmware-interpolated vertical gradient (stops bottom->top); b = the CLIP ZONE = the top
# 16 of the 121 LUT levels (~13% of the bar — size is firmware-fixed, armed by the op32
# VU-enable bit); c = the PEAK-CAP color (firmware derives its fade-to-black trail);
# a = LUT[0] / bar background. Dial slots are intent-built from config below; the button
# bank keeps the baseline-capture bytes.
OP34_HDR = bytes.fromhex("042100f8ffff02")          # a=0x2104 b=0xf800 c=0xffff cnt=2, verbatim
OP34_BTN_GREY = bytes.fromhex("10841084")           # button-bank colors, baseline f005 verbatim
N_STOPS = 16                                        # gradient stops the firmware LUT takes
CLIP_TOP = 16.0 / 121.0                             # clip-zone share of the full bar (fixed)

# bar colors: per-page `colors = [...]` accepts "#rgb", "#rrggbb", or a generic name below.
NAMED_COLORS = {
    "red": (230, 40, 40),     "orange": (235, 130, 40),  "amber": (255, 190, 0),
    "yellow": (240, 200, 40), "lime": (160, 230, 60),    "green": (60, 200, 80),
    "teal": (40, 160, 150),   "cyan": (60, 200, 220),    "blue": (60, 140, 240),
    "purple": (150, 80, 230), "violet": (150, 80, 230),  "magenta": (230, 60, 200),
    "pink": (240, 100, 180),  "discord": (114, 137, 218),
    "white": (255, 255, 255), "grey": (128, 128, 128),   "gray": (128, 128, 128),
}
DEFAULT_COLORS = ["orange", "red", "blue", "purple"]


def parse_color(s):
    """'#rgb' / '#rrggbb' / bare hex / generic name -> RGB565 (LE on the wire)."""
    s = str(s).strip().lower()
    if s in NAMED_COLORS:
        r, g, b = NAMED_COLORS[s]
    else:
        h = s.lstrip("#")
        if len(h) == 3 and all(c in "0123456789abcdef" for c in h):
            r, g, b = (int(c * 2, 16) for c in h)
        elif len(h) == 6 and all(c in "0123456789abcdef" for c in h):
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        else:
            raise ValueError("unknown color %r (use #rgb, #rrggbb or one of: %s)"
                             % (s, ", ".join(sorted(NAMED_COLORS))))
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def resample_stops(stops, n):
    """Linear-resample a gradient (list of RGB565, bottom->top) onto n stops, endpoints kept."""
    if n <= 0:
        return []
    if len(stops) == 1 or n == 1:
        return [stops[0]] * n
    out = []
    for j in range(n):
        x = j * (len(stops) - 1) / (n - 1)
        i = min(int(x), len(stops) - 2)
        f = x - i
        c0, c1 = stops[i], stops[i + 1]
        r = round(((c0 >> 11) & 31) * (1 - f) + ((c1 >> 11) & 31) * f)
        g = round(((c0 >> 5) & 63) * (1 - f) + ((c1 >> 5) & 63) * f)
        b = round((c0 & 31) * (1 - f) + (c1 & 31) * f)
        out.append((r << 11) | (g << 5) | b)
    return out


def op34_dial_ops(k, body, bg, clip, cap, band, band_from):
    """Dial slot k's intent-built op34: the lane's body gradient with the warning band on top.
    Every stop sitting at/above band_from percent of the FULL bar takes the band color, and
    the firmware-fixed clip zone above ~87% renders in `clip` — absolute bar positions, so
    the top of every bar warns identically regardless of the lane's body color."""
    span = (1.0 - CLIP_TOP) * 100.0 / (N_STOPS - 1)  # full-bar % between adjacent stops
    n_band = sum(1 for j in range(N_STOPS) if j * span >= band_from)
    stops = resample_stops(body, N_STOPS - n_band) + [band] * n_band
    return bytes([0x34, k & 3]) + struct.pack("<HHHB", bg, clip, cap, N_STOPS) + \
        b"".join(struct.pack("<H", v) for v in stops)


WAKE_CAP = "baseline-app-starting.pcapng"
VU_LMAX = 0x73                   # captured op40 levels span 0x00..0x73
# Peak-cap ("drop linger") ballistics, HW-proven by bars_synth: the firmware draws the cap
# from op40 L2/L3 and it is only VISIBLE while pk > cur — instant attack, HOLD at the peak,
# then a constant-rate fall. (A per-frame multiplicative decay glues pk to cur = no cap.)
VU_HOLD_S = 0.7                  # cap holds at the peak this long before falling
VU_FALL = 160.0                  # then falls at this many level-bytes per second
STALL_S = 0.5                    # loop gap that means the panel has likely blanked
SYNC_S = 2.0                     # periodic OS-state re-sync of the current page
TOUCH_GUARD_S = 0.4              # ignore readbacks for a lane this soon after a local detent
VOL_LABEL_S = 1.0                # while turning, the lane label shows "NN%"; revert after this


VERSION = "dev"                  # stamped with the release version by packaging/build-appimage.sh
CONFIG_VERSION = 1               # config schema; missing keys always take built-in defaults,
                                 # so this only gates future changes to EXISTING keys' meaning
MAX_LOGS = 10                    # session logs kept in logs/ (oldest pruned at startup)


class Dbg:
    """Run log: ONE file per session, logs/ui-YYYYmmdd-HHMMSS.log, oldest pruned beyond
    MAX_LOGS. Always written (health/STATS lines, errors, watchdog, meter binds,
    external-override warnings); the heavy per-report/per-frame I/O trace goes in too with
    --debug. Line-buffered; selftest runs without a file."""

    def __init__(self, trace, logfile=True):
        self.trace = trace
        self.t0 = time.monotonic()
        self.n = {}                          # counters for the periodic stats line
        self.f = None
        if logfile:
            os.makedirs(LOGS, exist_ok=True)
            self.path = os.path.join(LOGS, "ui-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
            self.f = open(self.path, "w", buffering=1)
            print("log -> %s%s" % (self.path, " (+ full I/O trace)" if trace else ""))
            try:
                for legacy in ("ui.log", "ui-debug.log"):    # pre-rotation scheme leftovers
                    p = os.path.join(LOGS, legacy)
                    if os.path.exists(p):
                        os.unlink(p)
                runs = sorted(f for f in os.listdir(LOGS)    # ui-<timestamp>.log, oldest first
                              if f.startswith("ui-") and f.endswith(".log") and f[3].isdigit())
                for f_ in runs[:-MAX_LOGS]:
                    os.unlink(os.path.join(LOGS, f_))
            except OSError:
                pass

    def _write(self, fmt, a):
        if self.f:
            self.f.write("%9.3f %s\n" % (time.monotonic() - self.t0, (fmt % a) if a else fmt))

    def __call__(self, fmt, *a):             # trace level — only recorded with --debug
        if self.trace:
            self._write(fmt, a)

    def log(self, fmt, *a):                  # always recorded
        self._write(fmt, a)

    def count(self, key, inc=1):
        self.n[key] = self.n.get(key, 0) + inc

    def stats(self):
        self.log("STATS %s", " ".join("%s=%d" % kv for kv in sorted(self.n.items())))


# --------------------------------------------------------------------------- frame builders

def elem_frame(op, row, slot, px, w, h):
    """One element blit (op35 icon / op36 label) as a single SM frame."""
    rle = bytes(ce.rle_encode(px, w, h))
    ops = bytes([op, row, slot]) + struct.pack("<H", len(rle)) + rle + b"\x00"
    return sm.pack_sm(ops)


def op41_ops(ch, pct, muted):
    """Volume readout ops for one lane, both banks (dial + button). Muted = grey colorway
    via id bits 5/6 (the app's 61/e1 ids in page-change.pcapng f008)."""
    val = max(0, min(0xFFFF, int(pct) * 0xFFFF // 100))
    grey = 0x60 if muted else 0
    ops = b""
    for bank in (0x00, 0x80):
        ops += bytes([0x41, bank | grey | (ch & 3)]) + struct.pack("<HH", val, val)
    return ops


def op30_ops(ch, muted, blink):
    """Channel/button-LED state: 1 = on, 2 = blink (muted lanes, only if mute_blink)."""
    return bytes([0x30, ch & 3, 2 if (muted and blink) else 1])


def wake_lite():
    """Wake/init frames, VERBATIM and in order: the baseline capture cut at its first
    op35/op36 element draw. Keeps the session/config/wake sequence that brings the render
    pipeline up (URBs ~0..32 incl. op10/11/12/14, op30/32/34/38, brightness) but never paints
    the Windows app's own icons/labels — the first content on glass is our page burst.

    The frames are EMBEDDED in src/wake_data.py (generated by dev/src-re/make_wake.py from the
    dev/pcap capture) — the runtime is self-contained, no data files, no dev/ needed."""
    import wake_data
    return wake_data.frames()


def frame_ops(f):
    """Opcode list of an authored/captured frame (for the debug TX log)."""
    ops = op_walk.sm_ops(f)
    if ops is None:
        return []
    try:
        return [op for _, op, _ in op_walk.walk(ops)]
    except (ValueError, IndexError, struct.error):
        return [-1]


# --------------------------------------------------------------------------- asset rendering

class Renderer:
    """icons/ asset -> element pixels (icons/README.md documents the names). All rasterizing
    happens at startup (pre-render); nothing in the hot loop touches PIL/rsvg."""

    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont     # only needed at startup
        self.Image, self.ImageDraw = Image, ImageDraw
        self.font = (ImageFont.truetype(LBL_FONT, LBL_SIZE) if os.path.exists(LBL_FONT)
                     else ImageFont.load_default())
        self.index = {}                                  # lower stem -> path (first hit wins)
        # icons/ (shipped, user-facing) wins; dev/media/named is the dev-machine fallback
        for top in (ICONS, os.path.join(MEDIA, "named")):
            if not os.path.isdir(top):
                continue
            for base, _dirs, files in os.walk(top):
                for f in files:
                    stem = os.path.splitext(f)[0].lower()
                    self.index.setdefault(stem, os.path.join(base, f))

    def resolve(self, name):
        """Icon reference -> file path. Two forms:
        - explicit FILE PATH (contains a '/'): absolute, ~user, $VAR, or repo-root-relative;
        - bare NAME: looked up in the icons/ stem index (then *_24/*_32 variants)."""
        p = os.path.expanduser(os.path.expandvars(name))
        if os.path.sep in p:
            for cand in (p, os.path.join(ROOT, p)):
                if os.path.isfile(cand):
                    return cand
            return None                      # explicit path: no stem-index fallback
        low = name.lower()
        for cand in (low, os.path.splitext(low)[0], low + "_24", low + "_32"):
            if cand in self.index:
                return self.index[cand]
        return None

    def _rasterize_svg(self, path, w, h):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
            tmp = t.name
        subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h), path, "-o", tmp],
                       check=True, capture_output=True)
        im = self.Image.open(tmp).convert("RGBA")
        os.unlink(tmp)
        return im

    @staticmethod
    def to_px(im):
        """Opaque RGB (composited on black) -> 20-bit element pixels: pure black -> 0 (RLE
        background run, like captured elements), else opaque-nib | RGB565. Compositing on
        black BEFORE conversion keeps the antialias greys the captured elements have."""
        raw = im.tobytes()                   # RGB byte stream
        out = []
        for i in range(0, len(raw), 3):
            r, g, b = raw[i:i + 3]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out.append((OPQ | v) if v else 0)
        return out

    def _on_black(self, rgba):
        bg = self.Image.new("RGB", rgba.size, (0, 0, 0))
        bg.paste(rgba, (0, 0), rgba)
        return bg

    def icon_px(self, name):
        """name/path -> 32x32 element pixels; '' or unresolvable -> fully transparent (clears
        the slot)."""
        path = self.resolve(name) if name else None
        if name and path is None:
            print("  (icon %s %r not found — clearing slot)" %
                  ("file" if os.path.sep in name else "name", name))
        if path is None:
            return [0] * (W_ICON * H_ICON)
        if path.lower().endswith(".svg"):
            im = self._rasterize_svg(path, W_ICON, H_ICON)
        else:
            im = self.Image.open(path).convert("RGBA")
            im.thumbnail((W_ICON, H_ICON))
        canvas = self.Image.new("RGBA", (W_ICON, H_ICON), (0, 0, 0, 0))
        canvas.paste(im, ((W_ICON - im.width) // 2, (H_ICON - im.height) // 2), im)
        return self.to_px(self._on_black(canvas))

    def label_px(self, text):
        """text -> 110x16 element pixels. Style matched to captured op36 labels (see module
        docstring + recon/labelref/compare2.png): SemiBold 14 on black, bbox-centered,
        luminance boost for the hinted white core, AA greys preserved."""
        im = self.Image.new("RGB", (W_LBL, H_LBL), (0, 0, 0))
        if text:
            dr = self.ImageDraw.Draw(im)
            t = text
            bb = dr.textbbox((0, 0), t, font=self.font)
            while t and bb[2] - bb[0] > W_LBL - 4:
                t = t[:-1]
                bb = dr.textbbox((0, 0), t, font=self.font)
            dr.text(((W_LBL - (bb[2] - bb[0])) / 2 - bb[0],
                     (H_LBL - (bb[3] - bb[1])) / 2 - bb[1]),
                    t, font=self.font, fill=(255, 255, 255))
            if LBL_BOOST != 1.0:
                im = im.point(lambda v: min(255, int(v * LBL_BOOST)))
        return self.to_px(im)


def render_page_elements(rend, pg):
    """Pre-render one page's 8 icons + 8 labels into element frames (capture order: dial row
    then button row). Defaults: label = lane match string; missing/'' icon clears the slot."""
    lanes = (pg.get("lanes", []) + [""] * 4)[:4]
    frames = []
    for row, icons_key, labels_key, deflabels in (
            (0, "icons", "labels", lanes),
            (1, "button_icons", "button_labels", [""] * 4)):
        icons = (pg.get(icons_key, []) + [""] * 4)[:4]
        labels = pg.get(labels_key)
        labels = ((labels if labels is not None else deflabels) + [""] * 4)[:4]
        for slot in range(4):
            frames.append(elem_frame(0x35, row, slot, rend.icon_px(icons[slot]), W_ICON, H_ICON))
        for slot in range(4):
            frames.append(elem_frame(0x36, row, slot, rend.label_px(labels[slot]), W_LBL, H_LBL))
    return frames


# --------------------------------------------------------------------------- audio worker

def _first_pct(text):
    for tok in text.replace("/", " ").split():
        if tok.endswith("%") and tok[:-1].isdigit():
            return int(tok[:-1])
    return None


def lane_state(match):
    """Read a lane's CURRENT volume/mute from PipeWire (read-only). Raw pct (can be >100).
    VU binding hints: "si" = tuple of ALL matched sink-input indices (apps like browsers run
    one stream per tab/media — volume fans out to all, so the VU meters all of them too);
    "dev" = parec device (default sink's monitor / a capture source for mic lanes)."""
    none = {"pct": 0, "muted": False, "si": None, "dev": None}
    mic = S.mic_match(match) if match else None
    if mic is not None:
        if mic == "":
            pct = _first_pct(S._pactl("get-source-volume", "@DEFAULT_SOURCE@"))
            muted = S._pactl("get-source-mute", "@DEFAULT_SOURCE@").strip().endswith("yes")
            return {"pct": pct if pct is not None else 50, "muted": muted,
                    "si": None, "dev": "@DEFAULT_SOURCE@"}
        hits = S.match_sources(mic)
        if hits:
            return {"pct": hits[0]["vol"] if hits[0]["vol"] is not None else 50,
                    "muted": hits[0]["mute"], "si": None, "dev": hits[0]["name"]}
        return none
    if match in ("default", "master"):
        pct = _first_pct(S._pactl("get-sink-volume", "@DEFAULT_SINK@"))
        muted = S._pactl("get-sink-mute", "@DEFAULT_SINK@").strip().endswith("yes")
        return {"pct": pct if pct is not None else 50, "muted": muted,
                "si": None, "dev": "@DEFAULT_MONITOR@"}
    hits = S.match_inputs(match) if match else []
    if hits:
        # meter only streams that are actually PLAYING (corked = paused = silent), and at
        # most 4 — a browser holds one stream per tab, no point tapping paused ones
        live = [si["index"] for si in hits if not si.get("corked")]
        return {"pct": hits[0]["vol"] if hits[0]["vol"] is not None else 50,
                "muted": hits[0]["mute"],
                "si": tuple(sorted(live)[:4]), "dev": None}
    return none


class AudioWorker(threading.Thread):
    """Owns every pactl subprocess call so the 20 ms display cadence never blocks on one.
    Commands (self.q):  ("vol", match, delta) — coalesced per lane, applied as
    read -> clamp(cur+delta, 0..100) -> set (OS authoritative, never above 100%);
    ("mute", match); ("button", action); ("sync", page_idx, [matches]);
    ("meter", lane_idx, key) — (re)spawn a VU Meter (parec fork happens HERE, off-cadence).
    Results (self.results): ("lane", match, pct, muted) | ("sync", page_idx, [states])
    | ("meter", lane_idx, key, Meter-or-None)."""

    def __init__(self, verbose, dbg):
        super().__init__(daemon=True)
        self.q = _queue.Queue()
        self.results = _queue.Queue()
        self.verbose = verbose
        self.dbg = dbg
        self.meter_factory = None            # set by UI.run() (needs bars_live/usb imports)

    def run(self):
        while True:
            item = self.q.get()
            if item is None:                 # sentinel: session teardown -> stop the thread
                return
            t0 = time.monotonic()
            try:
                self._handle(item)
                self.dbg("WORKER %-6s %s done in %.0fms", item[0], item[1:2],
                         (time.monotonic() - t0) * 1000)
            except Exception as e:
                print("  audio worker error (%s): %s" % (item[0], e))
                self.dbg("WORKER %s ERROR %s", item[0], e)

    def _set_clamped(self, match, delta):
        """read current -> clamp(cur+delta, 0..100) -> set. Caps the OS at 100% even though
        PipeWire allows 150%; an out-of-range OS value snaps back into 0..100 on first turn."""
        mic = S.mic_match(match)
        if mic is not None:
            if mic == "":
                cur = _first_pct(S._pactl("get-source-volume", "@DEFAULT_SOURCE@"))
                if cur is None:
                    return
                tgt = max(0, min(100, cur + delta))
                if tgt != cur:
                    S._pactl("set-source-volume", "@DEFAULT_SOURCE@", "%d%%" % tgt)
                # re-read: if something rewrote it (e.g. an app's auto-gain-control fighting
                # the knob — the "can't hit 50%" symptom), display the TRUTH and log it
                act = _first_pct(S._pactl("get-source-volume", "@DEFAULT_SOURCE@"))
                if act is not None and act != tgt:
                    self.dbg.log("OVERRIDE mic: set %d%% but OS now reads %d%% "
                                 "(external writer, e.g. app AGC?)", tgt, act)
                    tgt = act
                muted = S._pactl("get-source-mute", "@DEFAULT_SOURCE@").strip().endswith("yes")
                if self.verbose:
                    print("  default source %d%% -> %d%%" % (cur, tgt))
                self.results.put(("lane", match, tgt, muted))
                return
            shown = None
            for s in S.match_sources(mic):
                cur = s["vol"] if s["vol"] is not None else 0
                tgt = max(0, min(100, cur + delta))
                if tgt != cur:
                    S._pactl("set-source-volume", str(s["index"]), "%d%%" % tgt)
                if self.verbose:
                    print("  %s #%d %d%% -> %d%%" % (s["desc"] or s["name"], s["index"], cur, tgt))
                if shown is None:
                    shown = (tgt, s["mute"])
            if shown:
                self.results.put(("lane", match, shown[0], shown[1]))
            elif self.verbose:
                print("  (no capture source matches %r)" % mic)
            return
        if match in ("default", "master"):
            cur = _first_pct(S._pactl("get-sink-volume", "@DEFAULT_SINK@"))
            if cur is None:
                return
            tgt = max(0, min(100, cur + delta))
            if tgt != cur:
                S._pactl("set-sink-volume", "@DEFAULT_SINK@", "%d%%" % tgt)
            muted = S._pactl("get-sink-mute", "@DEFAULT_SINK@").strip().endswith("yes")
            if self.verbose:
                print("  default sink %d%% -> %d%%" % (cur, tgt))
            self.results.put(("lane", match, tgt, muted))
            return
        shown = None
        for si in S.match_inputs(match):
            cur = si["vol"] if si["vol"] is not None else 0
            tgt = max(0, min(100, cur + delta))
            if tgt != cur:
                S._pactl("set-sink-input-volume", str(si["index"]), "%d%%" % tgt)
            if self.verbose:
                print("  %s #%d %d%% -> %d%%" %
                      (si["app"] or si["binary"] or si["media"], si["index"], cur, tgt))
            if shown is None:
                shown = (tgt, si["mute"])
        if shown:
            self.results.put(("lane", match, shown[0], shown[1]))
        elif self.verbose:
            print("  (no sink-input matches %r)" % match)

    def _handle(self, item):
        kind = item[0]
        if kind == "vol":
            _, match, delta = item
            while True:                      # coalesce queued detents for the same lane
                try:
                    nxt = self.q.get_nowait()
                except _queue.Empty:
                    break
                if nxt[0] == "vol" and nxt[1] == match:
                    delta += nxt[2]
                else:
                    self.q.put(nxt)
                    break
            if delta:
                self._set_clamped(match, delta)
        elif kind == "setvol":               # absolute set (bounded-pot dials, e.g. 200 headset)
            _, match, target = item
            while True:                      # coalesce queued setvols for the same lane (keep last)
                try:
                    nxt = self.q.get_nowait()
                except _queue.Empty:
                    break
                if nxt[0] == "setvol" and nxt[1] == match:
                    target = nxt[2]
                else:
                    self.q.put(nxt)
                    break
            st = lane_state(match)           # reach the target via the clamped relative path
            if st["pct"] is not None:
                self._set_clamped(match, int(target) - int(st["pct"]))
        elif kind == "mute":
            S.mute_toggle(item[1], self.verbose)
            st = lane_state(item[1])         # read back the true state
            self.results.put(("lane", item[1], st["pct"], st["muted"]))
        elif kind == "button":
            S.do_button(item[1], self.verbose, 1, 0)   # non-page actions only
        elif kind == "sync":
            _, page_idx, matches = item
            self.results.put(("sync", page_idx, [lane_state(m) for m in matches]))
        elif kind == "meter":
            _, k, key = item
            self.results.put(("meter", k, key,
                              self.meter_factory(key) if key and self.meter_factory else None))
        elif kind == "close":                # reap parec here, never on the cadence thread
            for m in item[1] or ():
                m.close()


# --------------------------------------------------------------------------- pulse events

class PulseEvents(threading.Thread):
    """`pactl subscribe` listener: sets .dirty on sink-input/source lifecycle events so the
    main loop can trigger a lane re-sync within ~0.3 s instead of the 2 s poll. This is the
    fix for VU misattribution: when a monitored stream dies, PipeWire RE-LINKS the parec tap
    to a sink monitor (verified — it then hears EVERYTHING); the tap can only be replaced by
    a re-sync, so re-sync must happen promptly on stream death/birth."""

    def __init__(self, dbg):
        super().__init__(daemon=True)
        self.dbg = dbg
        self.dirty = False
        self.removed = []                    # sink-input indices seen in 'remove' events
        self.proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True,
                                     preexec_fn=__import__("bars_live")._die_with_parent)

    def run(self):
        for line in self.proc.stdout:        # ends when pactl dies / we get killed
            if "sink-input" in line:
                if "'new'" in line or "'remove'" in line or "'change'" in line:
                    self.dirty = True
                if "'remove'" in line:       # a dead stream = its tap is now RELINKED — the
                    try:                     # index lets the main loop kill that tap at once
                        self.removed.append(int(line.rsplit("#", 1)[1]))
                    except ValueError:
                        pass
            elif "source" in line and "source-output" not in line:
                if "'new'" in line or "'remove'" in line or "'change'" in line:
                    self.dirty = True
        self.dbg.log("PULSE-EV subscribe stream ended")

    def take_removed(self):
        if not self.removed:
            return ()
        r, self.removed = self.removed, []
        return r

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=0.5)
        except Exception:
            pass


# --------------------------------------------------------------------------- input reader

class InputReader(threading.Thread):
    """Blocking EP 0x81 reads (1 s timeout — the pattern stream100.py `run` proved reliable;
    short-timeout polling cancels transfers ~50x/s and can wedge the endpoint). Raw reports go
    into self.reports; decode stays in the main thread. Keeps liveness/error counters for the
    [mon] line and tries clear_halt() to recover the endpoint after backend errors."""

    def __init__(self, dev, dbg):
        super().__init__(daemon=True)
        import usb.core
        self._usberror = usb.core.USBError
        self.dev = dev
        self.dbg = dbg
        self.reports = _queue.Queue()
        self.n_reports = 0
        self.n_timeouts = 0
        self.errors = {}                     # errno -> count
        self.last_report = None              # monotonic ts of last report
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                buf = bytes(self.dev.read(S.EP_IN, S.REPORT_MAX, timeout=1000))
            except self._usberror as e:
                if e.errno == 110:           # idle — no events this second
                    self.n_timeouts += 1
                    continue
                key = str(e.errno)
                self.errors[key] = self.errors.get(key, 0) + 1
                self.dbg.log("IN-RD USBError errno=%s %s", e.errno, e)
                try:                         # recover a halted/wedged endpoint
                    self.dev.clear_halt(S.EP_IN)
                    self.dbg.log("IN-RD clear_halt(0x%02x) ok", S.EP_IN)
                except Exception as e2:
                    self.dbg.log("IN-RD clear_halt failed: %s", e2)
                time.sleep(0.1)
                continue
            self.n_reports += 1
            self.last_report = time.monotonic()
            self.dbg("IN  %s", buf[:12].hex())
            self.dbg.count("in_report")
            self.reports.put(buf)

    def status(self, now):
        age = "never" if self.last_report is None else "%.1fs ago" % (now - self.last_report)
        err = (" errs=" + ",".join("%s:%d" % kv for kv in sorted(self.errors.items()))
               if self.errors else "")
        return "%d reports (last %s) %d idle-timeouts%s" % (
            self.n_reports, age, self.n_timeouts, err)


class DeviceWatch(threading.Thread):
    """1 Hz USB presence poll, deliberately OFF the cadence: a libusb enumeration is blocking
    work and the 20 ms slot loop must never block. Sets .gone the moment the device disappears
    so the daemon can drop to tray-idle and resume on replug. ponytail: poll, not udev events
    — 1 Hz is ample for a human pulling a cable; switch to pyudev only if latency ever matters.
    Also flags .reload when config.toml's mtime changes (free: we're already polling off-cadence,
    and os.stat is a cheap syscall, not the blocking USB work the hot loop must avoid)."""

    def __init__(self, vid, pid, cfgpath=None):
        super().__init__(daemon=True)
        self.vid, self.pid = vid, pid
        self.gone = False
        self.stop = False
        self.cfgpath = cfgpath
        self.reload = False
        try:
            self._mtime = os.stat(cfgpath).st_mtime if cfgpath else None
        except OSError:
            self._mtime = None

    def run(self):
        import usbdev
        while not self.stop:
            try:
                gone = usbdev.find(idVendor=self.vid, idProduct=self.pid) is None
            except Exception:                        # probe error on a half-removed device == gone
                gone = True
            if gone:
                self.gone = True
                return
            if self.cfgpath:                         # config edited on disk? flag a reload
                try:
                    m = os.stat(self.cfgpath).st_mtime
                    if self._mtime is not None and m != self._mtime:
                        self.reload = True
                    self._mtime = m
                except OSError:                      # file mid-save / gone — try again next tick
                    pass
            time.sleep(1.0)


# --------------------------------------------------------------------------- the daemon

def single_instance():
    """One daemon per user: exclusive flock on a runtime-dir lockfile (repo checkout and
    AppImage hit the same file). The returned fd must stay open for the process lifetime;
    the kernel releases the lock on ANY exit, including SIGKILL."""
    import fcntl
    import tempfile
    rt = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    f = open(os.path.join(rt, "hercules-stream.lock"), "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        sys.exit("another instance is already running (pid %s) — it owns the device."
                 % (f.read().strip() or "?"))
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def spawn_tray(cfgpath, dbg, state_path=None):
    """Tray icon (src/tray.py) runs as a SEPARATE process — the 20 ms slot loop can't
    host an event loop. The child binds itself to our lifetime (PDEATHSIG) and exits
    silently when there is no session bus / dbus-next; the daemon runs fine trayless.
    state_path: a file where the daemon publishes 'idle'/'active' so the tray greys its
    icon while no device is attached (the daemon polls/writes it at session boundaries)."""
    icon = next((os.path.join(ICONS, n) for n in
                 ("hercules.png", "hercules.svg", "stream.png", "stream.svg")
                 if os.path.exists(os.path.join(ICONS, n))), "")
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray.py"),
           "--pid", str(os.getpid()), "--config", cfgpath or "",
           "--version", VERSION, "--icon", icon]
    if state_path:
        cmd += ["--state-file", state_path]
    try:
        os.makedirs(LOGS, exist_ok=True)
        err = open(os.path.join(LOGS, "tray.err"), "wb")   # why a tray didn't appear
        t = subprocess.Popen(cmd, stdout=err, stderr=err)
        dbg.log("tray spawned pid %d", t.pid)
        return t
    except Exception as e:
        print("  (tray not started: %s)" % e)
        return None


class UI:
    def __init__(self, cfg, dbg=None, cfgpath=None):
        st = cfg.get("settings", {})
        self.step = int(st.get("volume_step", 1))
        self.cps = max(1, int(st.get("counts_per_step", 6)))
        self.verbose = bool(st.get("verbose", True))
        self.pages = cfg.get("pages", [])
        if not self.pages:
            sys.exit("No [[pages]] defined in config.")
        ui = cfg.get("ui", {})
        self.brightness = max(0, min(100, int(ui.get("brightness", 80))))
        self.vu_on = bool(ui.get("vu", True))
        self.vu_gain = float(ui.get("vu_gain", 0.9))
        # body fall smoothing per 40 ms VU frame: 0 = raw peak (snappiest), 1 = frozen.
        # 0.45 ≈ 50 ms tail; the old 0.82 (≈200 ms tail at 25 Hz) felt laggy on HW.
        self.vu_release = min(0.95, max(0.0, float(ui.get("vu_release", 0.45))))
        # scale the bar by the lane's volume: full-scale audio tops out AT the volume level
        # (like the vendor UI), and a muted lane shows no bar. vu_scale=false = absolute.
        self.vu_scale = bool(ui.get("vu_scale", True))
        self.mute_blink = bool(ui.get("mute_blink", False))
        self.passes = max(1, int(ui.get("element_passes", 2)))
        self.tray = bool(ui.get("tray", True))
        self.cfgpath = cfgpath
        if "vu_map" in ui:
            print("note: [ui] vu_map is gone — bar k always meters lane k's own audio now.")
        self.dbg = dbg or Dbg(False, logfile=False)

        # VU bar color model ([ui]-configurable; defaults = the capture a/b/c values plus the
        # absolute warning bands: red clip zone on top, orange band from vu_band_from%).
        def ui_color(key, default):
            try:
                return parse_color(ui.get(key, default))
            except ValueError as e:
                print("  [ui] %s: %s — using default" % (key, e))
                return parse_color(default)
        self.vu_clip = ui_color("vu_clip_color", "#ff0000")   # top ~13% (zone size is firmware-fixed)
        self.vu_band = ui_color("vu_band_color", "orange")
        self.vu_band_from = max(0.0, min(100.0, float(ui.get("vu_band_from", 75))))
        self.vu_cap = ui_color("vu_cap_color", "#ffffff")     # peak cap (+ firmware fade trail)
        self.vu_bg = ui_color("vu_bg_color", "#202020")       # bar background

        self.page_colors = []                # per page: 4 x body gradient stops (op34)
        for pi, pg in enumerate(self.pages):
            cols, c565 = pg.get("colors", []), []
            for k in range(4):
                c = cols[k] if k < len(cols) and cols[k] else DEFAULT_COLORS[k]
                try:
                    stops = [parse_color(x) for x in (c if isinstance(c, list) else [c])]
                    if not 1 <= len(stops) <= N_STOPS:
                        raise ValueError("a gradient takes 1..%d colors" % N_STOPS)
                    c565.append(stops)
                except ValueError as e:
                    print("  page %d color %d: %s — using default" % (pi, k + 1, e))
                    c565.append([parse_color(DEFAULT_COLORS[k])])
            self.page_colors.append(c565)

        self.page = 0
        self.lanes = [{"pct": 50, "muted": False} for _ in range(4)]
        self.page_state = {}                 # page idx -> last known lane states
        self.queue = []                      # prebuilt frames (bursts) — one sent per slot
        self.dirty41 = set()                 # lane indices needing an op41 update
        self.dirty30 = set()                 # lane indices needing an op30 LED update
        self.page_elements = []              # per page: prerendered element frames
        self.worker = None                   # AudioWorker (run() only; selftest stays offline)
        self.last_touch = [0.0] * 4          # last local detent per lane (readback guard)
        self.last_sync = 0.0
        self.meters = [None] * 4             # VU: one Meter per lane (bar k = lane k's audio)
        self.meter_key = [None] * 4          # current binding: ("dev",name) | ("si",index)
        self.meter_pending = [None] * 4      # binding requested from the worker
        self.peaks = [0.0] * 4               # VU peak-cap level per lane (the linger marker)
        self.pk_t = [0.0] * 4                # when each lane's cap last attacked (hold timer)
        self.pk_prev = 0.0                   # previous VU frame time (fall-rate integration)
        self.last_vu = [0, 0, 0, 0]          # last sent body levels (for the [mon] line)
        self.rle_pct = None                  # prerendered "0%".."100%" label RLEs (op36)
        self.vol_until = [0.0] * 4           # lane label shows the pct until this time
        self.vol_shown = [None] * 4          # pct currently on the label (None = config label)
        self._rend = None                    # Renderer handle (set in run(); reload re-prerenders)
        self._reload_pending = False         # set when config.toml changed -> bounce the session

    # config-derived attributes the hot-reload copies from a freshly-built UI (everything set
    # above from `cfg`, MINUS prerender outputs (rebuilt via prerender()) and `tray` (spawned once,
    # can't toggle live). page_elements/rle_pct are NOT here — prerender() regenerates them.
    _CFG_ATTRS = ("step", "cps", "verbose", "pages", "brightness", "vu_on", "vu_gain",
                  "vu_release", "vu_scale", "mute_blink", "passes", "vu_clip", "vu_band",
                  "vu_band_from", "vu_cap", "vu_bg", "page_colors")

    def _reload_config(self):
        """Re-read config.toml and rebuild every config-derived attribute, then re-prerender.
        Build a throwaway UI from the new file FIRST and only commit its attrs if construction
        succeeds — a broken edit (bad TOML, no [[pages]]) thus leaves the running config intact
        instead of killing the daemon. The caller has already torn the session down and re-serves,
        so the worker/meters/lanes pick up the new pages/colors on bringup.

        ponytail: full rebuild + session bounce — every config key handled by one uniform path, so
        it can't get out of sync, at the cost of a panel blink during the bounce. Per-key live
        fast-paths (apply brightness / VU colours WITHOUT a bounce) are deliberately NOT built:
        they'd need a key->derived-state dependency map (e.g. a colour change must re-prerender that
        page's op35/op36 bytes), which is exactly the partial-update complexity this avoids. Add one
        only for a specific key if the blink on that tweak actually annoys someone — and keep the
        structural changes (pages/lanes) on this bounce path regardless."""
        try:
            fresh = UI(load_config(self.cfgpath), self.dbg, self.cfgpath)
        except (SystemExit, Exception) as e:   # SystemExit isn't an Exception; both = bad config
            print("  config reload skipped — keeping the running config (%s)" % e)
            self.dbg.log("config reload failed: %r", e)
            return False
        for a in self._CFG_ATTRS:
            setattr(self, a, getattr(fresh, a))
        self.page = min(self.page, len(self.pages) - 1)   # new config may have fewer pages
        self.prerender(self._rend)
        print("config reloaded from %s — re-serving" % self.cfgpath)
        self.dbg.log("config reloaded (%d pages)", len(self.pages))
        return True

    # ---- frame building ----

    def prerender(self, rend):
        self.page_elements = [render_page_elements(rend, pg) for pg in self.pages]
        # all 101 "NN%" labels as raw RLE (slot-independent) — the volume-linger label is
        # spliced into update frames without touching PIL in the hot loop
        self.rle_pct = [bytes(ce.rle_encode(rend.label_px("%d%%" % p), W_LBL, H_LBL))
                        for p in range(101)]

    def lane_matches(self, p=None):
        pg = self.pages[self.page if p is None else p]
        return (pg.get("lanes", []) + [""] * 4)[:4]

    def burst(self):
        """Page-switch burst, mirroring page-change.pcapng: config frame -> elements (xpasses)
        -> finish frame."""
        ops = bytearray()
        for ch in range(4):
            ops += bytes([0x30, ch, 0])
        ops += b"\x32" + OP32_PAGE
        for k in range(4):                   # dial slots: body gradient + warning band + clip/cap
            ops += op34_dial_ops(k, self.page_colors[self.page][k], self.vu_bg,
                                 self.vu_clip, self.vu_cap, self.vu_band, self.vu_band_from)
            ops += bytes([0x34, 0x80 | k]) + OP34_HDR + OP34_BTN_GREY
        for ch, lane in enumerate(self.lanes):
            ops += op41_ops(ch, lane["pct"], lane["muted"])
        ops += b"\x00"
        frames = [sm.pack_sm(bytes(ops))]
        for _ in range(self.passes):         # isoc is fire-and-forget: repaint for drops
            frames += self.page_elements[self.page]
        fin = bytearray()
        for ch, lane in enumerate(self.lanes):
            fin += op30_ops(ch, lane["muted"], self.mute_blink)
        fin += b"\x32" + OP32_IDLE + b"\x00"
        frames.append(sm.pack_sm(bytes(fin)))
        return frames

    def dirty_frame(self):
        now = time.monotonic()
        ops = bytearray()
        for k in sorted(self.dirty41):
            lane = self.lanes[k]
            ops += op41_ops(k, lane["pct"], lane["muted"])
            if self.rle_pct and now < self.vol_until[k]:   # label shows the live pct
                pct = max(0, min(100, lane["pct"]))
                if pct != self.vol_shown[k]:
                    rle = self.rle_pct[pct]
                    ops += bytes([0x36, 0, k & 3]) + struct.pack("<H", len(rle)) + rle
                    self.vol_shown[k] = pct
        for k in sorted(self.dirty30):
            ops += op30_ops(k, self.lanes[k]["muted"], self.mute_blink)
        self.dirty41.clear()
        self.dirty30.clear()
        ops += b"\x00"
        return sm.pack_sm(bytes(ops))

    def expire_vol_labels(self, now):
        """1 s after the last change, put the configured label back (from the page cache;
        labels of dial row k live at page_elements[page][4+k])."""
        for k in range(4):
            if self.vol_shown[k] is not None and now >= self.vol_until[k]:
                self.vol_shown[k] = None
                self.queue.append(self.page_elements[self.page][4 + k])

    # ---- VU meter binding (bar k = lane k's audio, always) ----

    def desired_meter_key(self, k, st):
        """Meter binding lane k should have, from its synced state: an explicit parec device
        (default-sink monitor / mic source) or the tuple of an app's sink-input indices
        (ALL of them — one Meter each, bar shows the max); absent -> None."""
        if not self.vu_on or not self.lane_matches()[k]:
            return None
        if st.get("dev"):
            return ("dev", st["dev"])
        return ("si", st["si"]) if st.get("si") else None

    def rebind_meter(self, k, key):
        """Request/drop lane k's meter when its binding changed (spawn happens in the worker)."""
        # Skip only if the desired key is already installed, or already being spawned. The
        # pending check must guard against meter_pending[k] being None: otherwise a desired
        # key of None matches "nothing pending" and short-circuits the drop below, stranding a
        # stale tap (a lane that lost its match kept metering @DEFAULT_MONITOR@ — wrong audio).
        if key == self.meter_key[k] or (self.meter_pending[k] is not None
                                        and key == self.meter_pending[k]):
            return
        if key is None:
            if self.meters[k]:
                self.worker.q.put(("close", self.meters[k]))
            self.meters[k] = None
            self.meter_key[k] = None
            self.meter_pending[k] = None
            self.peaks[k] = 0
            self.dbg.log("METER lane%d -> none", k)
        else:
            self.meter_pending[k] = key
            self.worker.q.put(("meter", k, key))
            self.dbg.log("METER lane%d request %s", k, key)

    # ---- state updates ----

    def _apply_lane(self, k, pct, muted):
        """Fold an OS readback/sync into lane k (display caps at 100)."""
        st = {"pct": min(100, int(pct)), "muted": bool(muted)}
        if st != self.lanes[k]:
            if st["muted"] != self.lanes[k]["muted"] and self.mute_blink:
                self.dirty30.add(k)
            self.lanes[k] = st
            self.dirty41.add(k)

    def drain_results(self):
        """Fold async worker results (volume readbacks, page syncs) into the display. A lane
        touched locally in the last TOUCH_GUARD_S keeps its predicted value — the next
        readback/sync corrects it."""
        if self.worker is None:
            return
        now = time.monotonic()
        while True:
            try:
                res = self.worker.results.get_nowait()
            except _queue.Empty:
                return
            if res[0] == "lane":
                _, match, pct, muted = res
                for k, m in enumerate(self.lane_matches()):
                    if m == match and now - self.last_touch[k] > TOUCH_GUARD_S:
                        self._apply_lane(k, pct, muted)
            elif res[0] == "sync" and res[1] == self.page:
                for k, st in enumerate(res[2]):
                    # a tap whose parec died (its device went invalid/suspended at spawn —
                    # e.g. the mic source mid-VAC-reconfig) keeps a live binding key but reads
                    # silence forever. App lanes self-heal because their sink-input index
                    # churns; a device key (mic/master) NEVER changes, so rebind_meter would
                    # early-return on it forever. Drop the dead binding here so the rebind
                    # below respawns it; reap off-cadence via the worker.
                    if self.meters[k] and any(m.dead() for m in self.meters[k]):
                        self.dbg.log("METER lane%d tap died -> drop+rebind", k)
                        self.worker.q.put(("close", self.meters[k]))
                        self.meters[k] = self.meter_key[k] = self.meter_pending[k] = None
                        self.peaks[k] = 0
                    self.rebind_meter(k, self.desired_meter_key(k, st))
                    if now - self.last_touch[k] > TOUCH_GUARD_S:
                        self._apply_lane(k, st["pct"], st["muted"])
            elif res[0] == "meter":
                _, k, key, mlist = res
                if self.meter_pending[k] == key:
                    if self.meters[k]:
                        self.worker.q.put(("close", self.meters[k]))
                    self.meters[k] = mlist
                    self.meter_key[k] = key if mlist else None
                    self.meter_pending[k] = None
                    self.peaks[k] = 0
                    self.dbg.log("METER lane%d bound %s x%d", k, key,
                                 len(mlist) if mlist else 0)
                elif mlist:                  # binding changed while spawning: discard
                    self.worker.q.put(("close", mlist))

    # ---- input events (MUST NOT block — pactl goes through the worker) ----

    def switch_page(self, newp):
        self.page_state[self.page] = self.lanes
        self.page = newp % len(self.pages)
        if self.verbose:
            print("page -> %s" % self.pages[self.page].get("name", self.page))
        # last known values immediately (display reacts now); fresh pactl sync lands async
        self.lanes = self.page_state.get(self.page,
                                         [{"pct": 50, "muted": False} for _ in range(4)])
        self.dirty41.clear()
        self.dirty30.clear()
        self.last_touch = [0.0] * 4
        self.vol_until = [0.0] * 4           # burst repaints the configured labels
        self.vol_shown = [None] * 4
        if self.worker:
            self.worker.q.put(("sync", self.page, self.lane_matches()))
        self.queue.extend(self.burst())

    def on_event(self, ev):
        self.dbg("EVENT %s", ev)
        self.dbg.count("ev_" + ev[0])
        lanes = self.lane_matches()
        if ev[0] == "encoder" and lanes[ev[1]]:
            k = ev[1]
            self.worker.q.put(("vol", lanes[k], self.step * ev[2]))
            # local prediction for instant feedback; the worker's readback corrects it
            self.lanes[k]["pct"] = max(0, min(100, self.lanes[k]["pct"] + self.step * ev[2]))
            self.dirty41.add(k)
            self.last_touch[k] = time.monotonic()
            self.vol_until[k] = self.last_touch[k] + VOL_LABEL_S   # label -> live "NN%"
        elif ev[0] == "encoder_btn" and lanes[ev[1]]:
            k = ev[1]
            self.worker.q.put(("mute", lanes[k]))
            self.lanes[k]["muted"] = not self.lanes[k]["muted"]
            self.dirty41.add(k)
            if self.mute_blink:
                self.dirty30.add(k)
            self.last_touch[k] = time.monotonic()
        elif ev[0] == "action_btn":
            btns = self.pages[self.page].get("buttons", [])
            if ev[1] >= len(btns):
                return
            action = btns[ev[1]]
            scheme, _, arg = action.partition(":")
            if scheme == "page":
                if arg == "next":
                    self.switch_page(self.page + 1)
                elif arg == "prev":
                    self.switch_page(self.page - 1)
                elif arg.isdigit():
                    self.switch_page(int(arg))
            elif scheme == "mute":
                self.worker.q.put(("mute", arg))   # readback will update a matching lane
            elif scheme == "cmd":
                self.worker.q.put(("button", action))

    # ---- main loop ----

    def run(self):
        """Supervisor loop. Renders the page assets and spawns the tray ONCE, then serves the
        device whenever one is attached. While NO device is attached the daemon idles in the
        tray with everything off — no audio worker, no input reader, no VU taps, no lane
        auto-routing — and brings the full session up (and tears it down) on hotplug.
        ponytail: Stream-100-only (polls e053); a 200 plugged into an idle daemon is ignored —
        the 200 backend is experimental and started from main() at launch, not hotplugged."""
        import signal

        def _term(_sig, _frm):               # systemd stop / kill -> clean shutdown path
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)

        rend = Renderer()
        self._rend = rend                    # kept so a config reload can re-prerender pages
        print("pre-rendering %d page(s) of elements..." % len(self.pages))
        self.prerender(rend)
        # The tray is the ONLY thing alive during tray-idle; it tracks our (stable) pid across
        # plug/unplug cycles, so spawn it ONCE here — not per session.
        import tempfile
        self._state_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "hercules-stream.state")
        self._write_state("idle")                    # greyed tray icon until a session starts
        tray = spawn_tray(self.cfgpath, self.dbg, self._state_path) if self.tray else None
        try:
            while True:
                dev, ep = self._wait_for_device()    # idle in the tray until a device attaches
                self._write_state("active")          # tray -> normal icon
                try:
                    self._serve(dev, ep)             # 20 ms cadence; returns when it unplugs
                except KeyboardInterrupt:
                    raise                            # quit -> outer handler (clean shutdown)
                except Exception as e:               # a session dies on USB/libusb removal in many
                    # ways; whatever it is, isolate it — NEVER let it kill the daemon (and the
                    # PDEATHSIG'd tray). _serve already tore the session down in its own finally.
                    print("  device session ended on error (%s) — back to tray idle" % e)
                    self.dbg.log("session error -> tray idle: %r", e)
                if self._reload_pending:             # config changed: rebuild + re-serve, no idle
                    self._reload_pending = False      # drop (the device is still attached). _serve
                    self._reload_config()             # already tore the session down; _wait_for_
                    continue                          # device re-opens it on the next loop.
                self._write_state("idle")            # tray -> greyed icon
                print("device removed — idling in the tray (audio/input/routing off); "
                      "replug to resume.")
        except KeyboardInterrupt:
            print("\nstopping.")
        finally:
            try:
                os.unlink(self._state_path)
            except OSError:
                pass
            if tray and tray.poll() is None:
                tray.terminate()

    def _wait_for_device(self):
        """Block — idling in the tray with all functionality off — until a Stream 100 is both
        attached AND openable, then return (dev, ep). SIGTERM (-> KeyboardInterrupt) quits."""
        import usbdev
        import display as D
        announced = False
        while True:
            try:
                present = usbdev.find(idVendor=D.VID, idProduct=D.PID) is not None
            except Exception as e:                   # a backend hiccup must not break the idle loop
                self.dbg.log("idle device probe error: %r", e)
                present = False
            if present:
                opened = self._open_device()
                if opened:
                    return opened
            if not announced:
                print("no Stream 100 attached — idling in the tray (audio/input/routing off). "
                      "Plug it in to start.")
                self.dbg.log("idle: waiting for a device")
                announced = True
            time.sleep(1.0)

    def _open_device(self):
        """Full device bring-up: USB reset (restores the power-on input state, as a real replug
        would) + display iface (open_ep) + input iface (iface0) claim. Returns (dev, ep), or
        None on any failure so the caller stays in tray-idle and retries on the next poll."""
        import usb.core
        import usb.util
        import usbdev
        import display as D
        dbg = self.dbg
        # Emulate a fresh replug: a USB reset restores the power-on state, so input is live
        # every session (a relaunch otherwise inherits the previous run's endpoint state).
        try:
            d0 = usbdev.find(idVendor=D.VID, idProduct=D.PID)
            if d0 is not None:
                d0.reset()
                usb.util.dispose_resources(d0)
                time.sleep(0.5)
                dbg.log("USB reset at open ok")
        except Exception as e:
            print("  (usb reset failed: %s)" % e)
            dbg.log("USB reset failed: %s", e)
        try:
            dev, ep = D.open_ep()            # sys.exits on not-found / iface-busy
        except SystemExit as e:
            dbg.log("open_ep failed (%s) — staying in tray idle, will retry", e)
            return None
        try:                                 # input side: claim iface0 on the SAME handle
            if dev.is_kernel_driver_active(S.IFACE):
                dev.detach_kernel_driver(S.IFACE)
        except Exception:
            pass
        try:
            usb.util.claim_interface(dev, S.IFACE)
        except usb.core.USBError as e:
            dbg.log("claim input iface failed (%s) — will retry", e)
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            return None
        try:
            dev.clear_halt(S.EP_IN)          # belt-and-braces: un-wedge the input endpoint
        except Exception as e:
            dbg("startup clear_halt: %s", e)
        return dev, ep

    def _write_state(self, state):
        """Publish daemon state ('idle' / 'active') for the tray (greyed vs normal icon). A tiny
        runtime file, written only at session boundaries — never in the 20 ms loop."""
        p = getattr(self, "_state_path", None)
        if not p:
            return
        try:
            with open(p, "w") as f:
                f.write(state)
        except OSError:
            pass

    def _serve(self, dev, ep):
        """One device session: bring up the audio worker / input reader / VU taps, paint the
        page, and run the 20 ms cadence until the device is unplugged (DeviceWatch) or we are
        told to quit. Tears down every per-session thread and the device handle on the way out;
        the tray (spawned in run()) outlives the session."""
        import usb.util
        import display as D
        import bars_live as BL
        dbg = self.dbg

        # Fresh per-session state so a replug starts clean (the process/tray persist).
        self.queue = []
        self.dirty41 = set()
        self.dirty30 = set()
        self.meters = [None] * 4
        self.meter_key = [None] * 4
        self.meter_pending = [None] * 4
        self.peaks = [0.0] * 4
        self.pk_t = [0.0] * 4
        self.last_touch = [0.0] * 4
        self.last_sync = 0.0
        self.vol_until = [0.0] * 4
        self.vol_shown = [None] * 4

        init_states = [lane_state(m) for m in self.lane_matches()]
        self.lanes = [{"pct": min(100, s["pct"]), "muted": s["muted"]} for s in init_states]
        self.worker = AudioWorker(self.verbose, dbg)
        rel = self.vu_release

        def factory(key):                    # -> LIST of Meters (multi-stream app lanes)
            if key[0] == "dev":
                return [BL.Meter(key[1], release=rel)]
            return [BL.Meter(monitor_stream=i, release=rel) for i in key[1]]
        self.worker.meter_factory = factory
        self.worker.start()

        if self.vu_on:                       # initial meters, pre-cadence (blocking is fine here)
            for k, st in enumerate(init_states):
                key = self.desired_meter_key(k, st)
                if key:
                    self.meters[k] = self.worker.meter_factory(key)
                    self.meter_key[k] = key
            print("VU on: bar k meters lane k (%s), gain=%.2f" %
                  (", ".join("%d:%s" % (k, self.meter_key[k] and self.meter_key[k][1])
                             for k in range(4)), self.vu_gain))

        wake = wake_lite()
        reader = InputReader(dev, dbg)
        reader.start()                       # capture input from t0; events drain after wake

        sched = D.Scheduler(ep, dev=dev)     # dev -> isoc-write removal guard (libusb abort fix)
        pulse_ev = PulseEvents(dbg)
        pulse_ev.start()
        devwatch = DeviceWatch(D.VID, D.PID, self.cfgpath)
        devwatch.start()                     # 1 Hz USB presence poll + config-mtime watch, OFF cadence
        istate = {"mask": 0, "pos": [None] * 4, "acc": [0] * 4, "cps": self.cps}
        print("wake-lite replay (%d init frames, app elements cut)..." % len(wake))
        for i in range(25):
            sched.send(sm.heartbeat())
        for u in wake:
            sched.send(u)

        self.queue.extend(self.burst())
        self.queue.append(sm.brightness(self.brightness))
        print("page '%s' queued (%d frames), brightness=%d. Ctrl-C to stop." %
              (self.pages[self.page].get("name", 0), len(self.queue), self.brightness))

        last = time.monotonic()
        last_mon = last
        try:
            while True:
                if devwatch.gone or sched.device_gone:   # unplugged -> end session, drop to idle
                    # sched.device_gone is the FAST path: the isoc-write guard trips on the very
                    # next slot after removal (before libusb can abort); devwatch is the backup.
                    print("  device removed — stopping audio/input/routing")
                    dbg.log("device removed -> tray idle")
                    break
                if devwatch.reload:          # config.toml edited -> bounce the session (panel blinks)
                    print("  config changed on disk — reloading (panel blinks briefly)")
                    dbg.log("config change -> reload")
                    self._reload_pending = True
                    break
                now = time.monotonic()
                if now - last > STALL_S:     # cadence broke -> panel has blanked; re-light it
                    print("  cadence stalled %.2fs — re-lighting panel" % (now - last))
                    dbg.log("WATCHDOG stall %.3fs -> relight", now - last)
                    dbg.count("watchdog")
                    self.queue = list(wake) + self.burst() + [sm.brightness(self.brightness)]
                elif now - last > 0.045:
                    dbg("SLOT overrun %.0fms", (now - last) * 1000)
                    dbg.count("overrun")
                last = now
                if now - last_mon > 10.0:    # always-on health line: is input alive?
                    if self.verbose:
                        print("[mon] input: %s | worker q=%d | vu=%s | tx %s" %
                              (reader.status(now), self.worker.q.qsize(), self.last_vu,
                               " ".join("%s=%d" % kv for kv in sorted(dbg.n.items())
                                        if kv[0].startswith("tx_"))))
                    dbg.stats()
                    last_mon = now

                while True:                  # drain decoded-input queue (reader thread fills it)
                    try:
                        buf = reader.reports.get_nowait()
                    except _queue.Empty:
                        break
                    if buf and buf[0] == S.FRAME_ID:
                        for ev in S.decode_events(buf, istate):
                            self.on_event(ev)
                self.drain_results()
                self.expire_vol_labels(now)
                # RELINK GUARD: a stream 'remove' means any tap bound to it is now relinked
                # to a sink monitor (hears EVERYTHING) — kill that lane's taps immediately;
                # the event-driven sync below rebinds the survivors within ~0.3 s.
                removed = pulse_ev.take_removed()
                if removed:
                    for k in range(4):
                        key = self.meter_key[k] or self.meter_pending[k]
                        if key and key[0] == "si" and any(i in key[1] for i in removed):
                            dbg.log("TAP-KILL lane%d: monitored stream(s) %s removed "
                                    "(relink guard)", k, [i for i in removed if i in key[1]])
                            if self.meters[k]:
                                self.worker.q.put(("close", self.meters[k]))
                            self.meters[k] = None
                            self.meter_key[k] = None
                            self.meter_pending[k] = None
                            self.peaks[k] = 0
                # re-sync: event-driven (stream born/died/changed -> within ~0.3 s; keeps a
                # relinked dead tap from showing another app's audio) + 2 s periodic fallback
                want_sync = (now - self.last_sync > SYNC_S
                             or (pulse_ev.dirty and now - self.last_sync > 0.3))
                if want_sync and self.worker.q.empty():
                    pulse_ev.dirty = False
                    self.worker.q.put(("sync", self.page, self.lane_matches()))
                    self.last_sync = now

                if self.queue:
                    f = self.queue.pop(0)
                    if dbg.trace:            # frame_ops walk is not free — trace only
                        dbg("TX  queued ops=%s len=%d (%d left)",
                            ["%02x" % o for o in frame_ops(f)], len(f), len(self.queue))
                    dbg.count("tx_queue")
                    sched.send(f)
                elif self.dirty41 or self.dirty30:
                    f = self.dirty_frame()
                    if dbg.trace:
                        dbg("TX  dirty ops=%s", ["%02x" % o for o in frame_ops(f)])
                    dbg.count("tx_dirty")
                    sched.send(f)
                elif self.vu_on and sched.slot % 2 == 0:
                    chans = []               # bar k = lane k's own audio; no meter -> 0
                    for k in range(4):
                        mlist = self.meters[k]
                        cur = (BL.to_byte(max(m.peak() for m in mlist), self.vu_gain)
                               if mlist else 0)
                        if self.vu_scale:  # bar tops out at the lane's volume; muted = no bar
                            lane = self.lanes[k]
                            cur = 0 if lane["muted"] else cur * min(100, lane["pct"]) // 100
                        if cur >= self.peaks[k]:           # attack: cap jumps with the body
                            self.peaks[k] = float(cur)
                            self.pk_t[k] = now
                        elif now - self.pk_t[k] >= VU_HOLD_S:  # linger, then constant fall
                            self.peaks[k] = max(float(cur),
                                                self.peaks[k] - VU_FALL * (now - self.pk_prev))
                        chans.append((k, cur, cur, int(self.peaks[k]), int(self.peaks[k])))
                    self.pk_prev = now
                    self.last_vu = [c[1] for c in chans]   # surfaced in the [mon] line
                    dbg.count("tx_vu")
                    sched.send(sm.vu(chans))
                else:
                    dbg.count("tx_hb")
                    sched.send(sm.heartbeat())
        finally:                             # KeyboardInterrupt propagates to run()'s supervisor
            dbg.stats()
            reader.stop = True
            devwatch.stop = True
            pulse_ev.close()
            for mlist in self.meters:
                for m in mlist or ():
                    m.close()
            while True:                      # meters still in flight from the worker
                try:
                    res = self.worker.results.get_nowait()
                except _queue.Empty:
                    break
                if res[0] == "meter":
                    for m in res[3] or ():
                        m.close()
            self.worker.q.put(None)          # stop the worker thread (no leak across replugs)
            for iface in (D.IFACE, S.IFACE):
                try:
                    usb.util.release_interface(dev, iface)
                except Exception:            # device already gone on unplug -> release is moot
                    pass
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            print("done (panel blanks without heartbeats).")


# --------------------------------------------------------------------------- selftest / cli

def selftest(cfg):
    """Offline: every authored frame must pass the firmware-mirror CRC validator AND parse
    cleanly through the complete op grammar (op_walk), and its URB must be HERC + Nx952."""
    ui = UI(cfg)
    ui.lanes = [{"pct": p, "muted": m} for p, m in
                ((70, False), (55, True), (0, False), (100, False))]
    try:
        rend = Renderer()
        ui.prerender(rend)
        mode = "real assets"
    except Exception as e:
        print("  (PIL/asset render unavailable: %s — synthetic elements)" % e)
        solid = [OPQ | 0xF81F] * (W_ICON * H_ICON)
        band = [OPQ | 0x07E0] * (W_LBL * H_LBL)
        ui.page_elements = [
            [elem_frame(0x35, r, s, solid, W_ICON, H_ICON) for r in (0, 1) for s in range(4)] +
            [elem_frame(0x36, r, s, band, W_LBL, H_LBL) for r in (0, 1) for s in range(4)]
            for _ in ui.pages]
        ui.rle_pct = [bytes(ce.rle_encode(band, W_LBL, H_LBL))] * 101
        mode = "synthetic"

    frames = []
    for p in range(len(ui.pages)):
        ui.page = p
        frames += [("page%d burst" % p, f) for f in ui.burst()]
    ui.dirty41 = {0, 1}
    ui.dirty30 = {1}
    frames.append(("dirty op41/op30", ui.dirty_frame()))
    ui.dirty41 = {2}
    ui.vol_until[2] = time.monotonic() + 5   # volume-linger active: op41 + spliced "NN%" op36
    frames.append(("dirty op41 + vol-label op36", ui.dirty_frame()))
    frames.append(("vu 4ch", sm.vu([(b, 30, 30, 60, 60) for b in range(4)])))
    frames.append(("brightness", sm.brightness(ui.brightness)))
    frames.append(("heartbeat", sm.heartbeat()))

    ok = True
    opscount = {}
    for name, f in frames:
        v = vu_crc.validate(f)
        ln = struct.unpack_from("<H", f, 954)[0]
        good_size = (len(f) - 952) % 952 == 0 and len(f) - 952 >= ln
        try:
            recs = list(op_walk.walk(f[960:952 + ln]))
            for _, op, _pl in recs:
                opscount[op] = opscount.get(op, 0) + 1
        except (ValueError, IndexError, struct.error) as e:
            print("  WALK FAIL [%s]: %s" % (name, e))
            ok = False
            continue
        if v != 0 or not good_size:
            print("  FAIL [%s]: crc=%d urb=%d sm=%d" % (name, v, len(f), ln))
            ok = False
    print("  %d frames (%s), ops histogram: %s" %
          (len(frames), mode, " ".join("%02x:%d" % (k, v) for k, v in sorted(opscount.items()))))
    wl = wake_lite()
    no_el = all(op not in (0x35, 0x36)
                for u in wl if op_walk.sm_ops(u)
                for _, op, _ in op_walk.walk(op_walk.sm_ops(u)))
    print("  wake-lite: %d frames, element-free=%s" % (len(wl), no_el))
    ok = ok and no_el

    # hot-reload safety: a broken edit must keep the running config (no crash / no lost pages),
    # and a successful reload must clamp the current page into a possibly-shorter new page list.
    import tempfile
    ui._rend = rend if mode == "real assets" else None
    n_pages = len(ui.pages)
    bad = os.path.join(tempfile.gettempdir(), "hercules-reload-bad.toml")
    with open(bad, "w") as f:
        f.write("this is = not valid toml [[[")
    ui.cfgpath = bad
    assert ui._reload_config() is False, "bad config must be rejected"
    assert len(ui.pages) == n_pages, "rejected reload must keep the old pages"
    clamp = "bad-path only"
    example = os.path.join(ROOT, "config.example.toml")
    if mode == "real assets" and os.path.exists(example):
        ui.page = 999                        # force out-of-range vs whatever example defines
        ui.cfgpath = example
        assert ui._reload_config() is True, "valid reload must succeed"
        assert 0 <= ui.page < len(ui.pages), "reload must clamp page into range"
        clamp = "page clamped"
    print("  hot-reload: bad-config rejected, %s" % clamp)

    # rebind_meter must DROP a stale meter when the desired key becomes None — the lane lost
    # its match (e.g. a page switch off a 'default'/'master' lane onto an app lane with no live
    # stream). Regression: the dedup guard matched key==None==meter_pending and skipped the
    # drop, so the lane kept its old @DEFAULT_MONITOR@ tap forever and metered the wrong audio
    # (seen live: a 'game' lane stuck on the master monitor). A VAC churns lanes to "no match"
    # often, which is why it surfaced there.
    ui.dbg = type("D", (), {"log": lambda *a, **k: None})()
    ui.worker = type("W", (), {"q": type("Q", (), {"items": [],
                  "put": lambda s, x: s.items.append(x)})()})()
    ui.meters = [None] * 4; ui.meter_key = [None] * 4
    ui.meter_pending = [None] * 4; ui.peaks = [0] * 4
    ui.meters[3] = ["stale-tap"]; ui.meter_key[3] = ("dev", "@DEFAULT_MONITOR@")
    ui.rebind_meter(3, None)
    assert ui.meter_key[3] is None, "stale meter not dropped (key=%r)" % (ui.meter_key[3],)
    assert any(it[0] == "close" for it in ui.worker.q.items), "stale tap not closed"
    ui.worker.q.items.clear()
    ui.rebind_meter(2, None)                     # already-empty lane must stay a no-op (no churn)
    assert not ui.worker.q.items, "empty lane should be a no-op"
    print("  rebind_meter: drops stale meter on desired=None, no-ops an empty lane")

    print("ui selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def load_config(cfgpath):
    """Parse config.toml and apply build-time overrides. Raises on bad/missing TOML — callers
    decide what that means: main() exits at startup, the hot-reload keeps the running config."""
    import features                            # internal build overrides win over config.toml
    with open(cfgpath, "rb") as f:
        cfg = S.toml.load(f)
    return features.apply_overrides(cfg)


def main():
    args = sys.argv[1:]
    # Turn a C-level abort (e.g. a libusb assert on surprise removal) into a thread-stack dump
    # in logs/crash.log instead of a silent death — the daemon often runs detached. ~free, and
    # it already paid for itself once (the iso_write removal abort). Keep the handle for the
    # process lifetime (main() blocks in run() until exit) so the fd stays open for the handler.
    import faulthandler
    try:
        os.makedirs(LOGS, exist_ok=True)
        _crashlog = open(os.path.join(LOGS, "crash.log"), "a")
        faulthandler.enable(file=_crashlog)
    except Exception:
        pass
    cfgpath = config_path()                  # repo config.toml (dev) or XDG home (packaged)
    if "-c" in args:
        cfgpath = args[args.index("-c") + 1]
    elif "--config" in args:
        cfgpath = args[args.index("--config") + 1]
    selftest_mode = "--selftest" in args
    dev_desc = None                          # which supported device is attached (devices.Device)
    if not selftest_mode and "--no-preflight" not in args:
        import firstrun                      # dialogs for missing config / udev rule / device
        dev_desc = firstrun.preflight(cfgpath)
    elif not selftest_mode:
        import devices                       # --no-preflight: detect the variant without dialogs
        dev_desc = devices.detect()
    if not os.path.exists(cfgpath):
        if "--selftest" in args:
            cfgpath = os.path.join(ROOT, "config.example.toml")
        else:
            sys.exit("no config at %s\n  copy %s\n  there and edit lanes/icons (icons/README.md)"
                     % (cfgpath, os.path.join(ROOT, "config.example.toml")))
    if S.toml is None:
        sys.exit("No TOML parser (need Python 3.11+ or `pip install tomli`).")
    cfg = load_config(cfgpath)                # parse + apply build overrides (shared with reload)
    cv = int(cfg.get("config_version", 1))    # absent = v1; additive keys need no migration
    if cv > CONFIG_VERSION:
        print("note: config_version %d is newer than this build understands (%d) — "
              "unknown keys are ignored; compare with config.example.toml" % (cv, CONFIG_VERSION))
    if selftest_mode:
        sys.exit(selftest(cfg))
    # Experimental-backend gate: the Stream 200 XLR is OFF unless its feature flag is set
    # (a build baked with --set features.stream200=true, or [features] stream200 = true in the
    # user's config). Refuse before claiming the single-instance lock / the device.
    if (dev_desc is not None and dev_desc.kind == "stream200"
            and not features.enabled(cfg, "stream200")):
        sys.exit("detected %s, but Stream 200 XLR support is experimental and OFF in this "
                 "build.\nEnable it by setting\n  [features]\n  stream200 = true\nin %s "
                 "(or build with --set features.stream200=true), then rerun."
                 % (dev_desc.name, cfgpath))
    lock = single_instance()                 # held (fd open) until the process exits
    if dev_desc is not None and dev_desc.kind == "stream200":
        import stream200                      # different transport/protocol — its own daemon
        print("detected %s — starting the Stream 200 XLR daemon (experimental)." % dev_desc.name)
        stream200.run_daemon(cfg, cfgpath=cfgpath, debug="--debug" in args)
    else:
        UI(cfg, Dbg("--debug" in args), cfgpath=cfgpath).run()
    del lock


if __name__ == "__main__":
    main()
