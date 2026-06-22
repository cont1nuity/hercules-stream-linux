# Status

Implemented, hardware-verified state of Hercules Stream for Linux — **one line per shipped
feature, done things only**. Plans, TODOs, and reverse-engineering findings live in the
maintainer's private RE repo, not here. When you verify something on hardware, record it here; if
it's load-bearing wire-format, update [../CLAUDE.md](../CLAUDE.md)'s Architecture section too.

- **Runtime: feature-complete** (daemon v1) — input, pages, volume/mute, per-lane VU, colors,
  custom icons, mic lanes, multi-stream lanes, relink guard all hardware-confirmed.
- **AppImage packaging** ✅ — built + hardware-verified 2026-06-11.
- **XDG config/log homes** ✅.
- **First-run setup** ✅ — `src/firstrun.py`, pkexec/GUI flow verified live 2026-06-12.
- **Login autostart** ✅ — XDG autostart `.desktop` entry via the tray (default ON with opt-out,
  `src/tray.py`); confirmed working 2026-06-17.
- **Graphical config editor** ✅ — `src/configui.py` (Tkinter; see [RUNTIME.md](RUNTIME.md) →
  "Config editor"): live 480×272 preview, per-page lane/button editing, icon/match/colour
  pickers, add/remove pages, comment-preserving TOML save, and **Apply & Restart**
  (`src/daemonctl.py`). The on-panel live restart (brief blank → repaint) is hardware-confirmed
  (2026-06-17). Bundled into the AppImage (Tcl/Tk via AppRun).
- **VU bar colors: config-driven** ✅ (hardware-verified 2026-06-12) — per-lane body color or
  gradient (page `colors`, up to 16 stops), plus an absolute warning band and clip/cap/background
  colors (`[ui]` keys `vu_clip_color` / `vu_band_color` / `vu_band_from` / `vu_cap_color` /
  `vu_bg_color`).
- **Stream 200 XLR (06f8:e054): experimental, NOT hardware-verified** ⚠️ — the daemon auto-detects
  the variant (`src/devices.py`) and dispatches to `src/stream200.py` (its own bulk-IF3 transport
  + telemetry poll). Audio control (knobs/buttons → PipeWire) and config support for the extra
  buttons/dials (`[stream200]` section + config-editor **200 XLR** tab) are implemented; the tester
  supplies each control's byte/bit/offset (discovered by probing a real device) since
  they aren't auto-detected. The panel display is a stub (image payload still being decoded). No
  byte is verified on a real 200 XLR yet.
