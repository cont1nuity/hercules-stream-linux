# Status

Implemented, hardware-verified state of Hercules Stream for Linux — **one line per shipped
feature, done things only**. Plans, TODOs, and reverse-engineering findings live in the
maintainer's private RE repo, not here. When you verify something on hardware, record it here; if
it's load-bearing wire-format, update [PROTOCOL.md](PROTOCOL.md) too.

- **Runtime: feature-complete** (daemon v1) — input, pages, volume/mute, per-lane VU, colors,
  custom icons, mic lanes, multi-stream lanes, relink guard all hardware-confirmed.
- **Tray-idle + bidirectional hotplug** ✅ (hardware-verified 2026-06-23) — with no device the
  daemon idles in the tray, all functionality off (no audio worker / input / metering / lane
  matching); it brings a session up on attach, tears it down on removal, and resumes on replug.
  Fixed the libusb `abort()` this exposed: an isochronous write to a surprise-removed device makes
  pyusb pass a negative iso-packet count to `libusb_alloc_transfer` (SIGABRT, uncatchable) —
  `Scheduler.send` now gates every isoc write on the device's usbfs node still existing.
- **Config hot-reload** ✅ (hardware-verified 2026-06-24) — editing `config.toml` (by hand or via
  the config editor) applies live without a restart: `DeviceWatch` flags an mtime change on its
  1 Hz off-cadence poll, the session bounces, and the daemon re-serves the attached device (brief
  panel blink). A broken edit (bad TOML, no `[[pages]]`) is rejected and the running config kept.
  Shipped in v1.2.0.
- **Tray idle icon** ✅ — greyed/dimmed while idle, normal while serving; daemon publishes
  `idle`/`active` to `$XDG_RUNTIME_DIR/hercules-stream.state`, the tray polls it. The config
  editor's tray on/off toggle was removed (the `tray` key stays in config.toml and is honored).
- **Crash logging** ✅ — `faulthandler` dumps every thread's stack to `logs/crash.log` on a fatal
  signal (so a future C-level abort self-records instead of dying silently).
- **AppImage packaging** ✅ — built + hardware-verified 2026-06-11.
- **Git + GitHub** ✅ — public repo `cont1nuity/hercules-stream-linux` (GPL-3.0) + private
  `…-dev` for RE material; `.gitignore` keeps `dev/`/`config.toml`/build scratch out of the
  public tree (2026-06-22).
- **CI** ✅ — GitHub Actions runs the offline selftests (CRC gate + op grammar, no hardware) on
  every push/PR (`.github/workflows/ci.yml`).
- **Releases** ✅ — a `v*` tag builds the AppImage on ubuntu-22.04 and publishes a GitHub Release
  (`.github/workflows/release.yml`), notes auto-pulled from `CHANGELOG.md`
  (`packaging/changelog-section.sh`); idempotent publish makes re-tagging safe. **v1.0.0 shipped
  2026-06-22.**
- **CHANGELOG** ✅ — `CHANGELOG.md` (Keep a Changelog); each release's notes are the tagged
  version's section.
- **XDG config/log homes** ✅.
- **First-run setup** ✅ — `src/firstrun.py`, pkexec/GUI flow verified live 2026-06-12.
- **Login autostart** ✅ — XDG autostart `.desktop` entry via the tray (default ON with opt-out,
  `src/tray.py`); confirmed working 2026-06-17.
- **Graphical config editor** ✅ — `src/configui.py` (Tkinter; see [RUNTIME.md](RUNTIME.md) →
  "Config editor"): live 480×272 preview, per-page lane/button editing, icon/match/colour
  pickers, add/remove pages, comment-preserving TOML save, and **Apply & Restart**
  (`src/daemonctl.py`). The on-panel live restart (brief blank → repaint) is hardware-confirmed
  (2026-06-17). Bundled into the AppImage (Tcl/Tk via AppRun).
- **VU tap self-heal: dead `parec` respawned** ✅ (hardware-verified 2026-06-25) — the 2 s re-sync
  checks each meter's liveness (`Meter.dead()`) and drops a dead tap so it gets respawned; fixes a
  device lane (mic / master, whose binding key never changes) going permanently flat after its tap
  died on a momentarily-unresolved source (seen with a virtual-audio-cable reconfig).
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
