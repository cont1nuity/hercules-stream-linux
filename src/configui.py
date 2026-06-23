#!/usr/bin/env python3
"""Tkinter config editor — PROTOTYPE / look-and-feel pass.

Standalone GUI for editing config.toml: per-page lanes (match / icon / label / VU
colour) and action buttons, plus the [ui] display knobs, with a LIVE 480x272 panel
preview on the right. The preview reuses the daemon's own render path — real icon &
label bitmaps (ui.Renderer + codec_element.render) and the real named-colour palette
(ui.parse_color) — so icons/labels/colours match the device exactly.

Caveat: there are NO host-side screen-position constants (the firmware hardcodes where
each element lands on the 480x272 panel), so the *arrangement* below is an approximation
of the device layout, not pixel-exact. Icons, labels and colours are exact. VU bars are
drawn at sample levels — quiet lanes show only their own colour; the orange warning band
and the firmware-fixed red clip zone (top ~13%) appear only on a lane that peaks loud.

Run:  .venv/bin/python src/configui.py [--config PATH]
This prototype does not touch the running daemon — it only reads/writes the TOML.
Saving rewrites the file from values (drops comments). Apply changes on the device by
restarting the daemon (tray -> Restart).
"""
import argparse
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, colorchooser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tomllib as _toml
except ImportError:                                        # <3.11 source installs
    try:
        import tomli as _toml
    except ImportError:                                    # pragma: no cover
        _toml = None

from PIL import Image, ImageDraw, ImageTk                  # noqa: E402
import codec_element as ce                                 # noqa: E402
import daemonctl                                            # noqa: E402
from ui import Renderer, parse_color                        # noqa: E402
from paths import ROOT                                      # noqa: E402
try:
    from paths import config_path
except Exception:                                          # pragma: no cover
    config_path = None

PANEL_W, PANEL_H = 480, 272
SCALE = 1.3                                                # preview zoom
COLW = PANEL_W // 4
CLIP_FRAC = 16.0 / 121.0                                   # firmware-fixed clip-zone share (= ui.CLIP_TOP)

COLOR_NAMES = ["red", "orange", "amber", "yellow", "lime", "green", "teal", "cyan",
               "blue", "purple", "violet", "magenta", "pink", "discord", "white", "grey"]
SPECIAL_MATCHES = ["default", "master", "mic", "browser", "game"]
PAGE_ARGS = ["next", "prev", "1", "2", "3", "4"]
ACTION_SCHEMES = ["mute", "cmd", "page", "none"]

# The 200 XLR's MAIN area (4 dials + 4 action buttons) is configured per page in the page tabs,
# exactly like the Stream 100. Only the RIGHT-SIDE additions are configured here: 5 buttons + the
# headphone dial. They are fixed, labelled controls, so the editor shows them by name and you set
# only what each DOES (a button -> an action, the headset -> an audio lane). The telemetry
# byte/bit/offset is a hardware fact baked into src/stream200.py (BUTTON_BITS/DIAL_FIELDS), never
# edited here. (Layout confirmed by the maintainer, 2026-06-18. The buttons + headphone ring have
# status LEDs; driving those is future work — the op-0x08 LED output format is still undecoded.)
S200_BUTTONS = [("mute", "Mute"), ("link", "Link"), ("page", "Next page"),
                ("creator", "Creator"), ("audience", "Audience")]
S200_DIALS = [("headset", "Headset")]
# sample meter heights per lane: mostly quiet (only the lane's own colour), one loud
# enough to reach the warning band + clip zone — so orange/red read as a peak, not the norm
DEMO_LEVELS = [0.22, 0.96, 0.16, 0.40]

HEADER_TIPS = {
    "Match (app / token)": "Which audio this encoder controls: an app-name substring "
        "(case-insensitive), or a special token — default / master (the output sink), "
        "mic (the input source), browser, game — or a|b for alternatives.",
    "Icon": "Icon shown above the lane: pick a built-in name from the list, or click … "
        "to browse for a custom image (PNG/SVG/…).",
    "Label": "Text shown under the icon. Defaults to the match string if left blank.",
    "Colour / gradient": "VU bar body colour. A name, #rrggbb, or a comma list like "
        "'green, yellow' to make a vertical gradient (bottom → top). Click the swatch "
        "to pick from a colour wheel.",
    "Action": "Left dropdown = what the button does; right = its target. "
        "mute toggles a lane's mute (target = a match/token), cmd runs a shell command "
        "(target = the command), page switches page (target = next/prev/number). "
        "none (or a blank target) = does nothing.",
}
UI_TIPS = {
    "brightness": "Panel backlight 0–100 at startup. 0 powers the panel OFF.",
    "vu": "Show live VU meter bars — read-only metering of each lane's own audio.",
    "vu_scale": "Scale each bar by the lane's volume: a full signal tops out at the volume "
        "level (muted lane → no bar), like the vendor UI. Off = absolute level.",
    "mute_blink": "When a lane is muted, blink its action-button LED.",
    "vu_gain": "Input gain applied before metering — higher makes the bars react sooner.",
    "vu_release": "Bar fall smoothing per frame: 0 = raw/snappy, 0.95 = very slow.",
    "vu_band_from": "The orange warning band starts at this % of the FULL bar, up to the "
        "clip zone. 75 = a thin band near the top; lower = warns earlier. 100 = no band.",
    "vu_band_color": "Colour of the warning band (the loud zone below the clip zone).",
    "vu_clip_color": "Colour of the clip zone — the device-fixed top ~13% of every bar "
        "(starts ~87%; the zone size is firmware-fixed, not configurable).",
    "vu_cap_color": "Peak-cap marker colour; on the device its falling trail fades to black.",
    "vu_bg_color": "Unlit bar background — the track behind the current level.",
}


# ---------------------------------------------------------------- small widgets


class Tooltip:
    """Hover tooltip — pure Tk, no dependency."""

    def __init__(self, widget, text, delay=450):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = self.after_id = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _=None):
        self._cancel()
        self.after_id = self.w.after(self.delay, self._show)

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + 18
        y = self.w.winfo_rooty() + self.w.winfo_height() + 4
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=320, padx=6, pady=4,
                 font=("TkDefaultFont", 9)).pack()

    def _leave(self, _=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _cancel(self):
        if self.after_id:
            self.w.after_cancel(self.after_id)
            self.after_id = None


def tip(widget, text):
    if text:
        Tooltip(widget, text)


class ScrollFrame(ttk.Frame):
    """A frame whose content scrolls (v + h) — guarantees the editor is never clipped,
    whatever the window/screen size. Put content in `.inner`."""

    def __init__(self, parent):
        super().__init__(parent)
        c = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=c.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=c.xview)
        self.inner = ttk.Frame(c)
        self.inner.bind("<Configure>", lambda e: c.configure(scrollregion=c.bbox("all")))
        c.create_window((0, 0), window=self.inner, anchor="nw")
        c.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        c.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._c = c
        c.bind("<Enter>", lambda e: c.bind_all("<MouseWheel>", self._wheel))
        c.bind("<Leave>", lambda e: c.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        self._c.yview_scroll(int(-(e.delta) / 120), "units")


# ---------------------------------------------------------------- discovery


def scan_icons():
    out, seen = [], set()
    try:
        for f in sorted(os.listdir(os.path.join(ROOT, "icons"))):
            stem, ext = os.path.splitext(f)
            if ext.lower() in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".bmp") and stem not in seen:
                seen.add(stem)
                out.append(stem)
    except Exception:
        pass
    return out


def match_candidates():
    vals = list(SPECIAL_MATCHES)
    low = {v for v in vals}
    try:
        import stream100
        for si in stream100.sink_inputs():
            for k in ("app", "binary"):
                v = (si.get(k) or "").strip()
                if v and v.lower() not in low:
                    low.add(v.lower())
                    vals.append(v)
    except Exception:
        pass
    return vals


# ---------------------------------------------------------------- render helpers


def rgb_of(spec):
    try:
        v565 = parse_color(spec)
    except Exception:
        v565 = parse_color("grey")
    return ce.rgb565(0xF0000 | v565)


def hex_of(spec):
    return "#%02x%02x%02x" % rgb_of(spec)


def lerp_stops(stops, t):
    if t <= 0 or len(stops) == 1:
        return stops[0]
    if t >= 1:
        return stops[-1]
    seg = t * (len(stops) - 1)
    i = int(seg)
    f = seg - i
    a, b = stops[i], stops[min(i + 1, len(stops) - 1)]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def elem_image(px, w, h):
    rgb = ce.render(px, w, h).convert("RGB")
    mask = Image.new("L", (w, h))
    mask.putdata([255 if v else 0 for v in px])
    rgb.putalpha(mask)
    return rgb


def spec_from_text(text):
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else "grey"
    return parts


def text_from_spec(spec):
    return ", ".join(spec) if isinstance(spec, list) else str(spec or "")


def first_stop(spec):
    s = spec_from_text(spec)
    return s[0] if isinstance(s, list) else s


def is_noop(action):
    return (action or "").strip().lower() in ("", "none")


def parse_action(s):
    """'scheme:arg' / 'none' / '' -> (scheme, arg)."""
    s = (s or "").strip()
    if ":" in s:
        sch, arg = s.split(":", 1)
        sch = sch.strip().lower()
        if sch in ("mute", "cmd", "page"):
            return sch, arg.strip()
    return "none", ""


def compose_action(scheme, arg):
    scheme = (scheme or "none").strip().lower()
    arg = (arg or "").strip()
    if scheme in ("", "none") or not arg:     # incomplete ≡ none
        return "none"
    return "%s:%s" % (scheme, arg)


# ---------------------------------------------------------------- TOML writer


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    raise TypeError("unserialisable: %r" % (v,))


def _is_aot(v):
    return isinstance(v, list) and bool(v) and isinstance(v[0], dict)


def _scalar_items(d):
    """(key, value) pairs that are scalars or scalar-lists — not nested tables/array-of-tables."""
    return [(k, v) for k, v in d.items() if not isinstance(v, dict) and not _is_aot(v)]


def _aot_items(d):
    """(key, value) pairs whose value is an array-of-tables (e.g. stream200 -> buttons/dials)."""
    return [(k, v) for k, v in d.items() if _is_aot(v)]


def _emit_aot_item(name, item):
    """A single [[name]] block (scalar keys only)."""
    return ["", "[[%s]]" % name] + ["%s = %s" % (k, _fmt(v)) for k, v in _scalar_items(item)]


def toml_dump(d):
    """Serialise the whole dict from scratch (drops comments). Used for a brand-new file,
    and as the guaranteed value-correct fallback when a comment-preserving merge can't be
    verified. Handles nested array-of-tables, e.g. [stream200] with [[stream200.buttons]]."""
    lines = ["%s = %s" % (k, _fmt(v)) for k, v in _scalar_items(d)]
    for k, v in d.items():
        if isinstance(v, dict):
            lines += _emit_table(k, v)
        elif _is_aot(v):
            for item in v:
                lines += _emit_aot_item(k, item)
    return "\n".join(lines).lstrip("\n") + "\n"


def _emit_table(name, d, aot=False):
    """A [name] table (scalar keys) followed by its nested [[name.key]] AOT blocks. aot=True
    emits a single [[name]] item instead (the pages-append path)."""
    if aot:
        return _emit_aot_item(name, d)
    out = ["", "[%s]" % name] + ["%s = %s" % (k, _fmt(v)) for k, v in _scalar_items(d)]
    for k, v in _aot_items(d):
        for item in v:
            out += _emit_aot_item("%s.%s" % (name, k), item)
    return out


# ---------------------------------------------------------------- config load


def default_cfg_path():
    if config_path:
        try:
            p = config_path()
            if os.path.exists(p):
                return p
        except Exception:
            pass
    for c in (os.path.join(ROOT, "config.toml"), os.path.join(ROOT, "config.example.toml")):
        if os.path.exists(c):
            return c
    return os.path.join(ROOT, "config.toml")


def load_cfg(path):
    with open(path, "rb") as f:
        return _toml.load(f)


def L4(seq, fill=""):
    seq = list(seq or [])
    return (seq + [fill] * 4)[:4]


def blank_page(n):
    return {"name": "Page %d" % n,
            "lanes": ["default", "", "", ""], "buttons": ["none"] * 4,
            "icons": ["volume", "", "", ""], "labels": ["Master", "", "", ""],
            "colors": ["blue", "grey", "grey", "grey"],
            "button_icons": [""] * 4, "button_labels": [""] * 4}


# ---------------------------------------------------------------- the app


class ConfigUI:
    def __init__(self, root, cfg, path):
        self.root = root
        self.cfg = cfg
        self.path = path
        self.cfg.setdefault("ui", {})
        self.cfg.setdefault("pages", [])
        self._had_s200 = "stream200" in self.cfg      # keep the section if it was on disk
        self._rend = None
        self._pending = None
        self._preview_imgtk = None
        self.last_page = 0                       # the page the preview tracks (sticky on Display)
        self.icon_values = scan_icons()
        self.match_values = match_candidates()

        root.title("Hercules Stream — Configuration  (prototype)")
        root.geometry("1340x800")
        root.minsize(1060, 620)

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)

        right = ttk.Frame(main)
        right.pack(side="right", fill="y", padx=(10, 0))
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        self.nb = ttk.Notebook(left)
        self.nb.pack(fill="both", expand=True)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(6, 0))
        self.btn_add = ttk.Button(bar, text="+  Add page", command=self.add_page)
        self.btn_add.pack(side="left")
        self.btn_del = ttk.Button(bar, text="–  Delete page", command=self.delete_page)
        self.btn_del.pack(side="left", padx=6)

        self.page_vars = []
        for pg in self.cfg["pages"]:
            self._build_page_tab(pg)
        self._build_display_tab()
        self._build_stream200_tab()
        self._build_preview(right)

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)   # bind AFTER build
        self.nb.select(0)
        self._update_page_buttons()
        self.refresh_preview()

    @property
    def rend(self):
        if self._rend is None:
            self._rend = Renderer()
        return self._rend

    # ------------------------------------------------------------ field widgets

    def _entry(self, parent, var, width=14):
        e = ttk.Entry(parent, textvariable=var, width=width)
        var.trace_add("write", self.schedule)
        return e

    def _combo(self, parent, var, values, width):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width)
        var.trace_add("write", self.schedule)
        return cb

    def _icon_field(self, parent, var):
        fr = ttk.Frame(parent)
        ttk.Combobox(fr, textvariable=var, values=[""] + self.icon_values,
                     width=10).pack(side="left")
        ttk.Button(fr, text="…", width=2,
                   command=lambda v=var: self._browse_icon(v)).pack(side="left", padx=(2, 0))
        var.trace_add("write", self.schedule)
        return fr

    def _browse_icon(self, var):
        p = filedialog.askopenfilename(
            title="Choose a custom icon image",
            filetypes=[("Images", "*.png *.svg *.jpg *.jpeg *.gif *.bmp"), ("All files", "*.*")])
        if p:
            var.set(p)

    def _color_field(self, parent, var, width=9):
        """Named-colour dropdown + a live swatch that opens a colour-wheel picker."""
        fr = ttk.Frame(parent)
        ttk.Combobox(fr, textvariable=var, values=COLOR_NAMES, width=width).pack(side="left")
        sw = tk.Canvas(fr, width=20, height=18, highlightthickness=1,
                       highlightbackground="#888", cursor="hand2")
        sw.pack(side="left", padx=(3, 0))

        def paint(*_):
            try:
                sw.configure(background=hex_of(first_stop(var.get())))
            except Exception:
                sw.configure(background="#000000")

        def pick(_=None):
            try:
                init = hex_of(first_stop(var.get()))
            except Exception:
                init = None
            c = colorchooser.askcolor(color=init, title="Pick a colour")
            if c and c[1]:
                var.set(c[1])

        sw.bind("<Button-1>", pick)
        var.trace_add("write", self.schedule)
        var.trace_add("write", paint)
        paint()
        return fr

    def _action_field(self, parent, var):
        """Two dropdowns: main scheme (mute/cmd/page/none) + a dependent sub-arg. The
        canonical 'scheme:arg' string is kept in `var` so save/preview are unchanged."""
        fr = ttk.Frame(parent)
        scheme, arg = parse_action(var.get())
        main = tk.StringVar(value=scheme)
        sub = tk.StringVar(value=arg)
        ttk.Combobox(fr, textvariable=main, values=ACTION_SCHEMES, width=6,
                     state="readonly").pack(side="left")
        sub_cb = ttk.Combobox(fr, textvariable=sub, width=15)
        sub_cb.pack(side="left", padx=(3, 0))

        def refresh_sub():
            s = main.get()
            if s == "mute":
                base = ["default", "master", "mic"]
                extra = [m for m in self.match_values if m.lower() not in base]
                sub_cb.configure(values=base + extra, state="normal")
            elif s == "page":
                sub_cb.configure(values=PAGE_ARGS, state="normal")
            elif s == "cmd":
                sub_cb.configure(values=[], state="normal")
            else:                                # none
                sub.set("")
                sub_cb.configure(values=[], state="disabled")

        def recompose(*_):
            var.set(compose_action(main.get(), sub.get()))

        def on_main(*_):
            refresh_sub()
            recompose()

        main.trace_add("write", on_main)
        sub.trace_add("write", recompose)
        var.trace_add("write", self.schedule)
        refresh_sub()                            # init sub state without clobbering var
        return fr

    # ------------------------------------------------------------ tab builders

    def _build_page_tab(self, pg):
        v = {
            "name": tk.StringVar(value=pg.get("name", "Page")),
            "lanes": [tk.StringVar(value=x) for x in L4(pg.get("lanes"))],
            "icons": [tk.StringVar(value=x) for x in L4(pg.get("icons"))],
            "labels": [tk.StringVar(value=x) for x in L4(pg.get("labels"))],
            "colors": [tk.StringVar(value=text_from_spec(x)) for x in L4(pg.get("colors", []))],
            "buttons": [tk.StringVar(value=x) for x in L4(pg.get("buttons"))],
            "button_icons": [tk.StringVar(value=x) for x in L4(pg.get("button_icons"))],
            "button_labels": [tk.StringVar(value=x) for x in L4(pg.get("button_labels"))],
        }
        self.page_vars.append(v)

        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text=pg.get("name", "Page"))
        f = ttk.Frame(sf.inner, padding=8)
        f.pack(fill="both", expand=True)

        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Page name").pack(side="left")
        self._entry(top, v["name"], width=20).pack(side="left", padx=6)

        lf = ttk.Labelframe(f, text="Lanes  (encoder 1 → 4)", padding=6)
        lf.pack(fill="x", pady=(8, 4))
        for c, h in enumerate(["#", "Match (app / token)", "Icon", "Label", "Colour / gradient"]):
            lab = ttk.Label(lf, text=h, font=("TkDefaultFont", 9, "bold"))
            lab.grid(row=0, column=c, padx=3, pady=2, sticky="w")
            tip(lab, HEADER_TIPS.get(h))
        for r in range(4):
            ttk.Label(lf, text=str(r + 1)).grid(row=r + 1, column=0, padx=3)
            self._combo(lf, v["lanes"][r], self.match_values, 16).grid(row=r + 1, column=1, padx=3, pady=1)
            self._icon_field(lf, v["icons"][r]).grid(row=r + 1, column=2, padx=3)
            self._entry(lf, v["labels"][r], 11).grid(row=r + 1, column=3, padx=3)
            self._color_field(lf, v["colors"][r]).grid(row=r + 1, column=4, padx=3)

        bf = ttk.Labelframe(f, text="Action buttons  (1 → 4)", padding=6)
        bf.pack(fill="x", pady=4)
        for c, h in enumerate(["#", "Action", "Icon", "Label"]):
            lab = ttk.Label(bf, text=h, font=("TkDefaultFont", 9, "bold"))
            lab.grid(row=0, column=c, padx=3, pady=2, sticky="w")
            tip(lab, HEADER_TIPS.get(h))
        for r in range(4):
            ttk.Label(bf, text=str(r + 1)).grid(row=r + 1, column=0, padx=3)
            self._action_field(bf, v["buttons"][r]).grid(row=r + 1, column=1, padx=3, pady=1)
            self._icon_field(bf, v["button_icons"][r]).grid(row=r + 1, column=2, padx=3)
            self._entry(bf, v["button_labels"][r], 11).grid(row=r + 1, column=3, padx=3)

        ttk.Label(f, text="Hover any column header for help. Empty action ≡ none.",
                  foreground="#777").pack(anchor="w", pady=(6, 0))

    def _build_display_tab(self):
        ui = self.cfg["ui"]
        self.ui_vars = {}
        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text="Display")
        f = ttk.Frame(sf.inner, padding=10)
        f.pack(fill="both", expand=True)

        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        bl = ttk.Label(row, text="Brightness", width=16)
        bl.pack(side="left")
        tip(bl, UI_TIPS["brightness"])
        bv = tk.IntVar(value=int(ui.get("brightness", 80)))
        self.ui_vars["brightness"] = bv
        ttk.Scale(row, from_=0, to=100, variable=bv,
                  command=lambda *_: self.schedule()).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(row, textvariable=bv, width=4).pack(side="left")

        tg = ttk.Frame(f)
        tg.pack(fill="x", pady=6)
        # [ui] tray is honored by the daemon and kept in config.toml, but intentionally NOT
        # exposed here — _sync_all merges into cfg["ui"], so the on-disk value round-trips
        # untouched. Edit the file to toggle the tray icon.
        for key, lbl, dflt in [("vu", "VU meters", True),
                               ("vu_scale", "Scale bar by volume", True),
                               ("mute_blink", "Mute → button LED blink", True)]:
            var = tk.BooleanVar(value=bool(ui.get(key, dflt)))
            self.ui_vars[key] = var
            var.trace_add("write", self.schedule)
            cbx = ttk.Checkbutton(tg, text=lbl, variable=var)
            cbx.pack(side="left", padx=(0, 14))
            tip(cbx, UI_TIPS.get(key))

        grid = ttk.Labelframe(f, text="VU colour model & gain", padding=8)
        grid.pack(fill="x", pady=8)
        fields = [("vu_gain", "Gain", 1.5), ("vu_release", "Release", 0.45),
                  ("vu_band_from", "Warning band from %", 75),
                  ("vu_band_color", "Warning band colour", "orange"),
                  ("vu_clip_color", "Clip colour (top ~13%, fixed)", "#ff0000"),
                  ("vu_cap_color", "Peak-cap colour", "#ffffff"),
                  ("vu_bg_color", "Bar background", "#202020")]
        for r, (key, lbl, dflt) in enumerate(fields):
            lab = ttk.Label(grid, text=lbl, width=24)
            lab.grid(row=r, column=0, sticky="w", pady=1)
            tip(lab, UI_TIPS.get(key))
            var = tk.StringVar(value=str(ui.get(key, dflt)))
            self.ui_vars[key] = var
            if key.endswith("_color"):
                self._color_field(grid, var, width=11).grid(row=r, column=1, sticky="w")
            else:
                var.trace_add("write", self.schedule)
                ttk.Entry(grid, textvariable=var, width=14).grid(row=r, column=1, sticky="w")

        ttk.Label(f, text="band/clip positions are % of the full bar. The red clip zone is "
                  "the firmware-fixed top ~13% (starts ~87%) — not adjustable.",
                  foreground="#777", wraplength=380, justify="left").pack(anchor="w", pady=(6, 0))

    def _s200_connected(self):
        """True if a Stream 200 XLR is currently attached (read-only USB enumeration)."""
        try:
            import devices
            d = devices.detect()
            return bool(d and d.kind == "stream200")
        except Exception:
            return False

    def _show_s200(self):
        """Show the 200 XLR tab only when one is attached, or the config already maps its
        controls (so an existing setup stays editable offline).

        Gated behind the `stream200` feature flag (experimental, OFF by default — a build baked
        with --set features.stream200=true, or [features] stream200 = true; see src/features.py):
        the tab stays hidden in a stock build even with a 200 plugged in.

        Hidden testing override: `[stream200] show_tab = true` in config.toml forces the tab on
        regardless — for the maintainer / testers / developers to edit the mapping on a machine
        without a 200 XLR. Undocumented in config.example.toml, off by default; the daemon
        ignores it."""
        s = self.cfg.get("stream200") or {}
        if s.get("show_tab"):
            return True
        try:
            import features
            if not features.enabled(self.cfg, "stream200"):
                return False
        except Exception:
            return False
        return self._s200_connected() or bool(s.get("buttons") or s.get("dials"))

    def _build_stream200_tab(self):
        """The Stream 200 XLR tab — shown only when a 200 XLR is attached (or already
        configured). Only the RIGHT-SIDE additions live here: the 5 fixed buttons (set an
        action each) and the headphone dial (set an audio lane). The 4 main dials + 4 action
        buttons are configured per page in the page tabs, exactly like the Stream 100. The
        telemetry byte/bit/offset is a hardware fact in src/stream200.py, not edited here."""
        if not self._show_s200():
            self.s200_vars = None
            return
        s = self.cfg.get("stream200") or {}
        by_b = {b.get("name"): b for b in (s.get("buttons") or []) if isinstance(b, dict)}
        by_d = {d.get("name"): d for d in (s.get("dials") or []) if isinstance(d, dict)}
        self.s200_vars = {"poll_hz": tk.StringVar(value=str(s.get("poll_hz", 50))),
                          "buttons": [], "dials": []}
        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text="200 XLR")
        f = ttk.Frame(sf.inner, padding=10)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Hercules Stream 200 XLR — extra controls  (experimental)",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        forced = bool(s.get("show_tab")) and not self._s200_connected()
        note = (("⚠ Testing view: forced on via [stream200] show_tab — no 200 XLR is "
                 "connected.\n" if forced else "") +
                "These are the right-side additions only. The 4 main dials and action buttons "
                "are set per page in the page tabs, exactly like the Stream 100. Set what each "
                "button does and which audio lane the headphone dial controls. The panel "
                "display isn't driven on the 200 yet — audio control only.")
        ttk.Label(f, text=note, foreground="#777", justify="left",
                  wraplength=560).pack(anchor="w", pady=(2, 8))

        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Poll rate (Hz)").pack(side="left")
        ttk.Entry(top, textvariable=self.s200_vars["poll_hz"], width=6).pack(side="left",
                                                                             padx=(4, 16))

        bf = ttk.Labelframe(f, text="Right-side buttons  (set what a press does)", padding=6)
        bf.pack(fill="x", pady=6)
        for c, h in enumerate(["Button", "Action"]):
            ttk.Label(bf, text=h, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=c, padx=3, pady=2, sticky="w")
        for r, (name, label) in enumerate(S200_BUTTONS):
            b = by_b.get(name, {})
            default = "page:next" if name == "page" else "none"
            rv = {"name": name,                          # fixed — the hardware label, not editable
                  "action": tk.StringVar(value=b.get("action", default))}
            self.s200_vars["buttons"].append(rv)
            ttk.Label(bf, text=label, width=12).grid(row=r + 1, column=0, padx=3, pady=1,
                                                     sticky="w")
            self._action_field(bf, rv["action"]).grid(row=r + 1, column=1, padx=3, sticky="w")

        df = ttk.Labelframe(f, text="Headphone dial  (set which audio lane it controls)",
                            padding=6)
        df.pack(fill="x", pady=6)
        for c, h in enumerate(["Dial", "Lane (audio match)", "Invert"]):
            ttk.Label(df, text=h, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=c, padx=3, pady=2, sticky="w")
        for r, (name, label) in enumerate(S200_DIALS):
            d = by_d.get(name, {})
            rv = {"name": name,                          # fixed physical dial, not editable
                  "lane": tk.StringVar(value=d.get("lane", "")),
                  "invert": tk.BooleanVar(value=bool(d.get("invert", False)))}
            self.s200_vars["dials"].append(rv)
            ttk.Label(df, text=label, width=12).grid(row=r + 1, column=0, padx=3, pady=1,
                                                     sticky="w")
            ttk.Combobox(df, textvariable=rv["lane"], values=self.match_values,
                         width=16).grid(row=r + 1, column=1, padx=3)
            ttk.Checkbutton(df, variable=rv["invert"]).grid(row=r + 1, column=2, padx=3)

        ttk.Label(f, text="Action = what a button press does (mute a lane / run a command / "
                  "switch page / none). The headphone dial drives its lane's volume.",
                  foreground="#777", wraplength=560, justify="left").pack(anchor="w",
                                                                          pady=(6, 0))

    def _build_preview(self, parent):
        ttk.Label(parent, text="Live panel preview",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(parent, width=int(PANEL_W * SCALE),
                                height=int(PANEL_H * SCALE), bg="#000000",
                                highlightthickness=1, highlightbackground="#444")
        self.canvas.pack(pady=6)
        ttk.Label(parent, text="bars at sample levels — orange/red show only where a lane "
                  "peaks; white line = peak cap", foreground="#777",
                  wraplength=int(PANEL_W * SCALE)).pack(anchor="w")

        b = ttk.Frame(parent)
        b.pack(fill="x", pady=(10, 0))
        ttk.Label(b, text="Config: %s" % self._short(self.path),
                  foreground="#777").pack(anchor="w", pady=(0, 6))
        ttk.Button(b, text="Revert", command=self.revert).pack(side="left")
        ttk.Button(b, text="Close", command=self.root.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(b, text="Apply & Restart", command=self.apply_restart).pack(side="right")
        ttk.Button(b, text="Apply (save)", command=self.apply).pack(side="right", padx=(0, 8))
        self.status = ttk.Label(parent, text="", foreground="#2a7",
                                wraplength=int(PANEL_W * SCALE), justify="left")
        self.status.pack(anchor="w", pady=4)

    # ------------------------------------------------------------ preview render

    def _short(self, p):
        h = os.path.expanduser("~")
        return p.replace(h, "~", 1) if p.startswith(h) else p

    def icon_img(self, name):
        name = (name or "").strip()
        if not name:
            return None
        try:
            return elem_image(self.rend.icon_px(name), 32, 32)
        except Exception:
            return None

    def label_img(self, text):
        try:
            return elem_image(self.rend.label_px(text or ""), 110, 16)
        except Exception:
            return None

    def _draw_bar(self, d, x0, ytop, x1, ybot, spec, ui, level):
        h = ybot - ytop
        d.rectangle([x0, ytop, x1, ybot], fill=rgb_of(ui.get("vu_bg_color", "#202020")))
        clip = rgb_of(ui.get("vu_clip_color", "#ff0000"))
        band = rgb_of(ui.get("vu_band_color", "orange"))
        cap = rgb_of(ui.get("vu_cap_color", "#ffffff"))
        try:
            band_from = max(0.0, min(1.0, float(ui.get("vu_band_from", 75)) / 100.0))
        except Exception:
            band_from = 0.75
        stops = [rgb_of(s) for s in (spec if isinstance(spec, list) else [spec])] or [(128, 128, 128)]
        clip_top = 1.0 - CLIP_FRAC                # firmware-fixed clip zone (top ~13%)
        for r in range(h):
            frac = 1.0 - r / float(h)
            if frac > level:                      # above the meter level → unlit track
                continue
            if frac >= clip_top:
                col = clip
            elif frac >= band_from:
                col = band
            else:
                col = lerp_stops(stops, frac / band_from if band_from > 0 else 0.0)
            d.line([(x0, ytop + r), (x1, ytop + r)], fill=col)
        cap_y = ybot - int(level * h)
        d.rectangle([x0, cap_y - 1, x1, cap_y + 1], fill=cap)

    def render_preview(self, page, ui):
        img = Image.new("RGB", (PANEL_W, PANEL_H), (14, 14, 18))
        d = ImageDraw.Draw(img)
        d.line([(0, 202), (PANEL_W, 202)], fill=(40, 40, 48))
        vu_on = bool(ui.get("vu", True))

        for c in range(4):
            cx = c * COLW + COLW // 2
            ic = self.icon_img(page["icons"][c])
            if ic:
                img.paste(ic, (cx - 16, 6), ic)
            lb = self.label_img(page["labels"][c] or page["lanes"][c])
            if lb:
                img.paste(lb, (c * COLW + 5, 40), lb)
            if vu_on:
                self._draw_bar(d, cx - 16, 62, cx + 16, 198,
                               spec_from_text(page["colors"][c]), ui, DEMO_LEVELS[c])
            else:
                d.rectangle([cx - 16, 62, cx + 16, 198],
                            fill=rgb_of(ui.get("vu_bg_color", "#202020")))

        for c in range(4):
            act = page["buttons"][c]
            x0, x1 = c * COLW + 8, c * COLW + COLW - 8
            empty = is_noop(act)
            d.rounded_rectangle([x0, 206, x1, 262], radius=6,
                                fill=(30, 30, 36) if empty else (44, 44, 54),
                                outline=(70, 70, 84))
            if not empty:
                cx = c * COLW + COLW // 2
                bic = self.icon_img(page["button_icons"][c])
                if bic:
                    img.paste(bic, (cx - 16, 210), bic)
                blb = self.label_img(page["button_labels"][c])
                if blb:
                    img.paste(blb, (c * COLW + 5, 242), blb)

        try:
            b = max(0, min(100, int(ui.get("brightness", 80)))) / 100.0
        except Exception:
            b = 0.8
        if b < 1.0:
            img = Image.eval(img, lambda px: int(px * b))
        return img.resize((int(PANEL_W * SCALE), int(PANEL_H * SCALE)), Image.NEAREST)

    # ------------------------------------------------------------ state sync

    def _page_dict(self, i):
        v = self.page_vars[i]
        return {
            "name": v["name"].get(),
            "lanes": [x.get() for x in v["lanes"]],
            "icons": [x.get() for x in v["icons"]],
            "labels": [x.get() for x in v["labels"]],
            "colors": [spec_from_text(x.get()) for x in v["colors"]],
            "buttons": [("none" if is_noop(x.get()) else x.get().strip()) for x in v["buttons"]],
            "button_icons": [x.get() for x in v["button_icons"]],
            "button_labels": [x.get() for x in v["button_labels"]],
        }

    def _ui_dict(self):
        out = {}
        for k, var in self.ui_vars.items():
            val = var.get()
            if k in ("vu_gain", "vu_release"):
                try:
                    val = float(val)
                except Exception:
                    pass
            elif k == "vu_band_from":
                try:
                    val = int(float(val))
                except Exception:
                    pass
            out[k] = val
        return out

    @staticmethod
    def _int_or(s, default=None):
        s = (s or "").strip()
        return int(s) if s.lstrip("-").isdigit() else default

    def _s200_dict(self):
        """Collect the Stream 200 tab into a [stream200] section dict — the RIGHT-SIDE additions
        only. Control names are FIXED (S200_BUTTONS/S200_DIALS). A button is written when it has
        a real action; the headphone dial when it has a lane. (byte/bit/offset are not here —
        that hardware map lives in src/stream200.py.)"""
        out = {"poll_hz": self._int_or(self.s200_vars["poll_hz"].get(), 50)}
        if (self.cfg.get("stream200") or {}).get("show_tab"):
            out["show_tab"] = True                      # preserve the hidden testing override
        buttons = []
        for rv in self.s200_vars["buttons"]:
            act = rv["action"].get().strip()
            if not is_noop(act):
                buttons.append({"name": rv["name"], "action": act})
        if buttons:
            out["buttons"] = buttons
        dials = []
        for rv in self.s200_vars["dials"]:
            lane = rv["lane"].get().strip()
            if lane:
                d = {"name": rv["name"], "lane": lane}
                if rv["invert"].get():
                    d["invert"] = True
                dials.append(d)
        if dials:
            out["dials"] = dials
        return out

    def _raw_tab_index(self):
        try:
            return self.nb.index(self.nb.select())
        except Exception:
            return None

    def _sync_all(self):
        for i in range(len(self.page_vars)):
            self.cfg["pages"][i].clear()
            self.cfg["pages"][i].update(self._page_dict(i))
        self.cfg["ui"].update(self._ui_dict())
        if self.s200_vars is not None:          # tab present (a 200 is attached / configured)
            s200 = self._s200_dict()
            # write [stream200] only if it has controls or was already on disk — don't add an
            # empty section to a pure Stream-100 config.
            if s200.get("buttons") or s200.get("dials") or self._had_s200:
                self.cfg["stream200"] = s200
            else:
                self.cfg.pop("stream200", None)
        # else: tab not shown -> leave any on-disk [stream200] untouched

    def schedule(self, *_):
        if self._pending:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(120, self.refresh_preview)

    def refresh_preview(self):
        self._pending = None
        if not self.page_vars:
            self.canvas.delete("all")
            return
        i = min(self.last_page, len(self.page_vars) - 1)    # sticky: Display keeps last page
        try:
            img = self.render_preview(self._page_dict(i), self._ui_dict())
        except Exception as e:
            self.status.config(text="preview error: %s" % e, foreground="#c33")
            return
        self._preview_imgtk = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._preview_imgtk)
        self.nb.tab(i, text=self.page_vars[i]["name"].get() or "Page")

    # ------------------------------------------------------------ tabs / page state

    def _on_tab(self, *_):
        i = self._raw_tab_index()
        if i is not None and i < len(self.page_vars):       # a page tab → remember it
            self.last_page = i
        self._update_page_buttons()
        self.schedule()

    def _update_page_buttons(self):
        i = self._raw_tab_index()
        on_display = i is not None and i >= len(self.page_vars)   # the Display tab
        st = "disabled" if on_display else "normal"
        self.btn_add.configure(state=st)
        self.btn_del.configure(state=st)

    def _rebuild(self, sel=0):
        for w in self.nb.winfo_children():
            w.destroy()
        self.page_vars = []
        for pg in self.cfg["pages"]:
            self._build_page_tab(pg)
        self._build_display_tab()
        self._build_stream200_tab()
        if self.cfg["pages"]:
            sel = min(sel, len(self.cfg["pages"]) - 1)
            self.last_page = sel
            self.nb.select(sel)
        self._update_page_buttons()
        self.refresh_preview()

    def add_page(self):
        self._sync_all()
        self.cfg["pages"].append(blank_page(len(self.cfg["pages"]) + 1))
        self._rebuild(len(self.cfg["pages"]) - 1)
        self.status.config(text="Added a page (unsaved).", foreground="#777")

    def delete_page(self):
        if len(self.cfg["pages"]) <= 1:
            messagebox.showinfo("Delete page", "Keep at least one page.")
            return
        cur = min(self.last_page, len(self.page_vars) - 1)
        if not messagebox.askyesno("Delete page",
                                   "Delete page '%s'?" % self.page_vars[cur]["name"].get()):
            return
        self._sync_all()
        del self.cfg["pages"][cur]
        self._rebuild(cur)
        self.status.config(text="Deleted a page (unsaved).", foreground="#777")

    # ------------------------------------------------------------ save / revert

    def _save(self):
        """Write the config from values (rewritten from scratch; comments are not kept).
        Returns True on success."""
        self._sync_all()
        try:
            if os.path.exists(self.path):
                import shutil
                shutil.copy2(self.path, self.path + ".bak")   # keep the previous file
            with open(self.path, "w") as f:
                f.write(toml_dump(self.cfg))
            return True
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return False

    def apply(self):
        if not self._save():
            return
        self.status.config(
            text="Saved %s (previous → .bak; rewritten from values). "
            "Restart the daemon (tray → Restart) to apply." % self._short(self.path),
            foreground="#2a7")

    def apply_restart(self):
        if not self._save():
            return
        pid = daemonctl.restart_daemon()
        if pid:
            self.status.config(
                text="Saved & restarting the daemon (pid %d) — the panel will blank briefly."
                % pid, foreground="#2a7")
        else:
            self.status.config(
                text="Saved, but found no running daemon to restart — start it to apply.",
                foreground="#a70")

    def revert(self):
        if not messagebox.askyesno("Revert", "Discard edits and reload from disk?"):
            return
        self.cfg = load_cfg(self.path)
        self.cfg.setdefault("ui", {})
        self.cfg.setdefault("pages", [])
        self._had_s200 = "stream200" in self.cfg
        self._rebuild(0)
        self.status.config(text="Reverted to on-disk config.", foreground="#777")


def main():
    ap = argparse.ArgumentParser(description="Hercules Stream config editor (prototype)")
    ap.add_argument("--config", help="path to config.toml")
    args = ap.parse_args()
    if _toml is None:
        sys.exit("needs Python 3.11+ (tomllib)")
    path = args.config or default_cfg_path()
    if not os.path.exists(path):
        sys.exit("config not found: %s" % path)
    cfg = load_cfg(path)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    ConfigUI(root, cfg, path)
    root.mainloop()


if __name__ == "__main__":
    main()
