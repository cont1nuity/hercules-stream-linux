# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) — and human contributors — when
working with code in this repository.

> **Maintainer note:** on the maintainer's machine a private reverse-engineering repo is nested
> at `dev/` (USB captures, decompiles, Ghidra projects, findings docs). It is not part of the
> public repo. **If `dev/` exists, also read `dev/CLAUDE.md`** — it holds the RE-side
> instructions. Everything in *this* file works without `dev/`.

## What this is

**Hercules Stream for Linux** — a Linux driver/daemon for the Hercules **Stream** family of
audio controllers (Guillemot Corp.), built by reverse-engineering the Windows-only *Hercules
Stream Control* app. Currently supported hardware: the **Stream 100** (USB `06f8:e053`); the
project naming is variant-neutral so further Stream models can land later.
**Both protocol halves are fully decoded and the product works:**

- **Input** (encoders, buttons, pages) — decoded in `src/stream100.py` (`decode_events()`).
- **Output / LCD** (4.3" 480×272 panel: backgrounds, icons, labels, VU bars, volume arcs,
  brightness, LED states) — wire format fully known; frames are built *from intent* by
  `src/sm.py` and validated against the op grammar in `src/op_walk.py`.
- **The daemon**: `src/ui.py` (launched via `./start.sh`) ties it together — knobs/buttons drive
  per-app PipeWire volume/mute, the panel shows pages of icons/labels/volume arcs, and per-lane
  VU bars meter each lane's own audio stream. It is **hotplug-aware**: with no device attached it
  idles in the tray with everything off (no audio worker / input / metering / lane matching) and
  brings the full session up on attach, tearing it down on removal and resuming on replug. **v1 is
  feature-complete with no known runtime bugs**; released as v1.0.0 — AppImage on GitHub Releases
  (see "Status & open items").

There is no build system — plain Python. The runtime is fully self-contained: `src/` + `icons/`
+ `fonts/` (the 32 verbatim panel wake/init frames are embedded in `src/wake_data.py`).

## The rules (non-negotiable)

**Don't guess protocol bytes.** Every byte on the wire is either *decoded and hardware-verified*
(build it from intent via `sm.py`) or a *capture constant* (verbatim bytes from USB captures of
the Windows app — e.g. the op34 `a/b/c` fields, op32 config bytes, and the page-switch burst in
`ui.py`). Treat capture constants as immutable: the captures live in the maintainer's private
repo, and every protocol regression in this project's history came from guessing. If you need a
byte's meaning, open an issue rather than inventing it.

**Every SM frame needs a valid CRC.** Any code that builds **or edits** an SM frame must
recompute the CRC-16 at SM[4:6] — the firmware silently drops bad-CRC frames before parsing.
The single chokepoint is `Scheduler.send()` in `src/display.py` (recomputes CRC + stamps seq on
every send); route all device traffic through it.

**Nothing in the 20 ms slot loop may block.** The panel has no framebuffer — if the heartbeat
cadence stalls the panel blanks (and heartbeats alone don't repaint it). All `pactl` calls,
meter spawns, and other blocking work belong in the worker threads, never inline in the send loop.

**An isochronous write to a removed device aborts the whole process.** On surprise removal pyusb
feeds libusb a negative iso-packet count → `libusb_alloc_transfer` asserts → `SIGABRT`
(uncatchable — no Python `except`, not even the supervisor's, can stop it). `Scheduler.send()`
gates every isoc write on the device's usbfs node still existing and stops cleanly (`device_gone`)
when it vanishes — never issue an isoc write without that guard.

**Never change system audio as a side-effect.** The daemon sets volumes only because the user
turned a knob (OS-authoritative: set → read back → display; capped at 100%). Metering is
read-only. Tests must save/restore any system setting they touch. (Setting volume to 100% once
during testing blasted a user's ears.)

## Setup & running

```bash
./setup.sh                  # venv (pyusb, pillow, dbus-next), udev rule (sudo once), config.toml from example; replug device
./start.sh                  # run the daemon (activates .venv, execs src/ui.py)
. .venv/bin/activate        # for running individual scripts below

# Offline selftests (no hardware needed) — run after touching frame-building code:
python3 src/ui.py --selftest       # full daemon frame validation (CRC-accept + op-grammar clean)
python3 src/sm.py                  # frame-builder acceptance tests (byte-match vs captured frames)
python3 src/vu_crc.py              # CRC-16 self-test
python3 src/element_test.py --selftest   # icon/label/slot-grid frames
python3 src/stream200.py selftest  # Stream 200 XLR transport/decoder/control-map (no device)
python3 src/sm200.py               # Stream 200 XLR display frame-builder (byte-match vs captured frames)

# Input side:
python3 src/stream100.py info      # device descriptor / endpoint dump
python3 src/stream100.py probe     # turn knobs / press buttons, watch byte-diffs
python3 src/stream100.py list      # list PipeWire sink-inputs (find config match strings)
python3 src/stream100.py run -c config.toml   # input-only mode: encoders/buttons → volume/mute

# Stream 200 XLR (06f8:e054) — EXPERIMENTAL, see "Status & open items":
python3 src/stream200.py info      # IF3 descriptors + a few telemetry polls
python3 src/stream200.py run -c config.toml    # poll telemetry → PipeWire (the 200 daemon)
# ui.py / ./start.sh auto-detect the variant (src/devices.py) and dispatch to the right daemon.

# Display side (manual tests):
python3 src/display.py [--secs 30] # wake panel + base layout, hold with heartbeats
python3 src/display.py --image x.png   # show a 480×272 image (background plane)

# Packaging:
packaging/build-appimage.sh [VERSION]  # -> dist/Hercules-Stream-Linux-<VERSION>-x86_64.AppImage
packaging/build-appimage.sh 1.0.0 --enable-stream200 --set ui.brightness=80  # bake build overrides
# Cut a release: move CHANGELOG.md [Unreleased] -> "## [X.Y.Z] - DATE", commit, then
#   git tag -a vX.Y.Z -m … && git push origin vX.Y.Z   # Actions builds the AppImage + publishes the Release
```

Python 3.9+ from source (3.11+ uses stdlib `tomllib`; older auto-installs `tomli` — the AppImage
bundles its own 3.12, so host Python is irrelevant there). Third-party deps: `pyusb` + `pillow` + `dbus-next` (tray) (+
system `libusb-1.0`; `rsvg-convert` for SVG icons; stdlib `tkinter` / system `tk` for the
graphical config editor). PipeWire `pactl` for the audio side. Config is `config.toml`
(copy from `config.example.toml`): pages, lane match strings (`"default"`, `"mic"`, app names,
`|` alternatives, aliases like `"browser"`/`"game"`), icons (built-in set in `icons/`, or any
image by path), labels, colors, `[ui]` knobs.

**Runtime, packaging & UI** — config/log homes, the AppImage build, build overrides & feature
flags, first-run preflight, the single-instance lock, the system tray, and the graphical config
editor (`src/configui.py`) are documented in **[docs/RUNTIME.md](docs/RUNTIME.md)**. Load-bearing
essentials to keep in mind: `src/paths.py` owns the dev-vs-packaged config/log switch
(`config_path()` / `LOGS`) — import from it, never hardcode paths; `src/features.py` resolves
settings/feature-flags as `build_overrides.toml` > `config.toml` > code defaults (the `stream200`
backend is gated OFF by default — `--enable-stream200` / `[features] stream200 = true`); the daemon holds an exclusive flock on
`$XDG_RUNTIME_DIR/hercules-stream.lock` and writes its pid there (one instance owns the device;
`src/daemonctl.py` uses it to find/restart the daemon); in the AppImage the bundled libusb is
pinned via `HERCULES_STREAM_LIBUSB`, **not** `LD_LIBRARY_PATH`, so spawned `pactl`/`parec` keep
resolving host libraries; the tray and config editor are separate processes (the 20 ms slot
loop must never host an event loop).

### Repo layout
- `src/` — all Python. `src/paths.py` is the single source of truth for directory locations;
  scripts resolve data dirs through it and run from any cwd. New scripts: `from paths import …`,
  don't hardcode paths.
- `icons/` — original, redistributable 18-icon set (`icons/README.md` documents naming/formats).
- `fonts/` — Noto Sans for label rendering (SIL OFL 1.1, see `fonts/OFL.txt`).
- `logs/` — daemon logs, one file per session (`ui-YYYYmmdd-HHMMSS.log`, 10 kept).
- `packaging/` — `build-appimage.sh` (see [docs/RUNTIME.md](docs/RUNTIME.md)). `build/` +
  `dist/` are its scratch/output (never commit them).
- `docs/` — reference docs read on demand: `RUNTIME.md` (config/log homes, packaging,
  first-run, single-instance, tray, config editor), `STATUS.md` (progress & open items).
- `start.sh` / `setup.sh` — entry point / installer. License: GPL-3.0-or-later (`LICENSE`).
- `dev/` — if present: the maintainer's private RE repo (see note at top), ignored by git here.

There is no hardware-independent test suite. The selftests above validate every generated frame
offline (CRC gate + op grammar); **anything visual is verified by a photo of the panel** — the
device is the only renderer that counts. If you change frame-building code, run the selftests;
if you change what's drawn, test on hardware.

## Architecture

### Transport (verified)
- **Interface 0** (vendor 0xFF): EP `0x02` bulk OUT = host→device commands; EP `0x81`
  interrupt IN = input events. No kernel driver binds → libusb claims directly.
- **Interface 1** alt 1: EP `0x01` isochronous OUT, **952-byte packets** = the LCD stream.

### Input report (`src/stream100.py`)
64-byte report: encoders = signed-16 absolute counters at bytes 3/5/7/9; buttons = bitmask at
byte 1 (low nibble = encoder pushes, high nibble = action buttons). `decode_events()` decodes.

### Display wire format (the load-bearing knowledge)
A logical frame on the isoc stream = `"HERCULES"`+pad sync packet, then 1..N 952-byte payload
packets (firmware length cap 0x2C9F):
```
"SM" | len:u16 LE | CRC16 | seq:u16 | ops…        (len = 8 + len(ops))
```
- **CRC-16 is a hard gate**: reflected, poly 0x8005, init 0, over SM[0:len) skipping bytes 4–5
  (the CRC field itself). Firmware drops bad-CRC frames *silently* before parsing. `vu_crc.py`
  implements it; `Scheduler.send()` applies it. ⚠️ This field was historically misread as a
  timestamp — it is not; never stamp a time there.
- **Frames are multi-op bundles.** `src/op_walk.py` is the authoritative grammar for every op
  (validated byte-exact against 9,946 captured frames). Don't hand-parse offsets — `op_walk.walk()`.
- Ops: **0x00** heartbeat · **0x30** `[ch][state]` channel LED (0 off / 1 on / 2 blink) ·
  **0x31** brightness 0..100 (0 = panel off) · **0x32** meter/page config (partially decoded —
  capture constants) · **0x33** palette upload · **0x34** per-slot meter config
  `[sel][a,b,c:u16][cnt][cnt×RGB565]` — `colors[cnt≤16]` = bar body, cnt≥2 = vertical gradient
  the firmware interpolates (stops bottom→top); `b` = clip-zone color (the top 16 of 121
  levels ≈ 13% of the bar, size firmware-fixed); `c` = peak-cap color (firmware fades its
  trail); `a` = bar background — hardware-verified 2026-06-12 · **0x35** icon 32×32 / **0x36** label 110×16 — RLE
  bitmaps addressed by `[row][column-slot]` into a fixed 2×4 grid; **no X/Y coordinates exist on
  the wire** (screen positions are hardcoded in firmware) · **0x37/0x38** background image:
  256-color palette + 32 tiles, each tile an *interlaced polyphase downsample* of the 480×272
  frame in an Adam7-style progressive order (the `PHASE` table in `src/codec_decode.py`) ·
  **0x40** VU bar levels · **0x41** volume display `[id][val:u16][val2:u16]` (percent + arc).
- **Two planes**: op37/38 own only the background; firmware composites the element layers
  (icons/labels, VU bars, button row) on top — a background repaint doesn't touch them.
- **No framebuffer.** The panel blanks the moment the ~20 ms heartbeat cadence stops.

### Module map
- `src/sm.py` — builds SM frames from intent (`heartbeat`/`brightness`/`vu`/`generic`,
  multi-packet `pack_sm`). `python3 src/sm.py` = acceptance tests.
- `src/op_walk.py` — op grammar / frame validator.
- `src/vu_crc.py` — the CRC (`fix_frame`, `validate`).
- `src/display.py` — `Scheduler` (20 ms cadence, owns seq + CRC on every send; gates each isoc
  write on the device's usbfs node so a surprise removal can't `abort()` the process — sets
  `device_gone`), panel wake, image display. `src/wake_data.py` — embedded verbatim wake frames.
- `src/codec_decode.py` / `codec_encode.py` — background image codec (palette + tile interlace).
  `src/codec_element.py` — icon/label/gauge RLE codec.
- `src/pcapdec.py` — dependency-free USBPcap/pcapng parser; the codec / `op_walk` / `display`
  modules and the offline selftests import it to validate against captured frames.
- `src/stream100.py` — input decode + PipeWire helpers (`sink_inputs` resolves app names via
  the client object for property-less streams, e.g. Spotify).
- `src/usbdev.py` — `usb.core.find` wrapper; honors `HERCULES_STREAM_LIBUSB` (AppImage bundled
  libusb). All daemon device-opens (`stream100.open_device`, `display.open_ep`, the startup
  reset in `ui.py`) go through it.
- `src/tray.py` — system-tray icon (SNI + DBusMenu over dbus-next; separate process; see
  "Tray" above). Greys its icon while the daemon is idle (no device): the daemon publishes
  `idle`/`active` to `$XDG_RUNTIME_DIR/hercules-stream.state` (passed as `--state-file`) and the
  tray polls it. (The config editor no longer exposes a tray on/off toggle — `tray` stays in
  `config.toml` and is still honored.)
- `src/configui.py` — Tkinter config editor with a live panel preview + comment-preserving
  TOML save (see [docs/RUNTIME.md](docs/RUNTIME.md)). `python3 src/configui.py` opens it standalone.
- `src/daemonctl.py` — find/restart the running daemon (reads its pid from the single-instance
  lock file, relaunches via the tray's mechanism); used by the editor's **Apply & Restart**.
- `src/bars_live.py` — non-blocking per-lane `parec` peak reader (`Meter` / `to_byte`); the
  daemon's live VU source (used by `ui.py`).
- `src/ui.py` — the daemon. `run()` is a **hotplug supervisor**: render assets + spawn the tray
  ONCE, then loop `_wait_for_device` (idle, everything off) → `_serve` (one device session) → idle
  again on removal; any session-fatal exception is isolated so it can never kill the daemon (the
  PDEATHSIG'd tray would die with it). Each `_serve` runs one 20 ms slot loop sending exactly one
  frame per slot (queue > dirty op41/op30 > op40 VU every 2nd slot > heartbeat). Threads keep the
  loop non-blocking: **InputReader** (blocking 1 s EP 0x81 reads), **AudioWorker** (owns all pactl;
  read→clamp→set→readback), **PulseEvents** (`pactl subscribe`; kills VU taps bound to removed
  streams instantly — a dead `parec --monitor-stream` tap gets relinked by PipeWire to a sink
  monitor and would meter everything), **DeviceWatch** (1 Hz usbfs presence poll, off the cadence,
  flags removal). VU = per-lane `parec` taps, volume-relative by default, instant-attack/hold/fall
  peak-cap ballistics. `main()` enables `faulthandler` → `logs/crash.log` (a C-level abort leaves a
  thread dump). `--debug` for trace logging.

## Status & open items

v1 is feature-complete and hardware-verified (input, pages, volume/mute, per-lane VU, colors,
icons); the graphical config editor and AppImage packaging are implemented, and **v1.0.0 is
published on GitHub Releases** — CI runs the offline selftests on every push, and a `v*` tag builds
the AppImage and publishes a Release with notes pulled from `CHANGELOG.md`. **Post-v1.0.0:**
device-less tray-idle + bidirectional hotplug (hardware-verified — unplug/replug no longer kills
the daemon; the prior libusb isoc-`abort()` on removal is fixed via the usbfs-node gate), a greyed
tray icon while idle, the tray toggle dropped from the config editor, and `faulthandler` crash
logging. Full status — what's verified on hardware, the VU-color model, op32/protocol minutiae, and
remaining (hardware-gated Stream 200) work — lives in **[docs/STATUS.md](docs/STATUS.md)**. When you
verify or disprove something on hardware, record it there; if it's load-bearing wire-format, update
the Architecture section above too.

**Stream 200 XLR (06f8:e054) — EXPERIMENTAL, NOT yet hardware-verified.** A second variant is
wired in so testers can run it: `src/devices.py` detects e053-vs-e054 and `ui.py`/`firstrun.py`
dispatch a 200 to `src/stream200.py` (its own bulk-IF3 transport + op-0x01 telemetry poll —
decoded/capture-constant only; the isoc/SM stack does NOT apply). **The 200 backend is gated
behind the `stream200` feature flag and is OFF by default** (`src/features.py`): a stock build
refuses a detected 200 with a hint to enable it. Turn it on with `[features] stream200 = true` in
config.toml, or ship a build with it on via `packaging/build-appimage.sh --enable-stream200`
(`--set features.stream200=true`). `features.py` is the general build-override layer —
`src/build_overrides.toml` (build-baked, optional) overrides config.toml at runtime; resolution is
`build_overrides.toml` > `config.toml` > code defaults. **Control model:** the 200's
MAIN area is identical to the 100 — 4 dials (each push-to-mute) + 4 action buttons, configured per
page via `[[pages]]` exactly like the 100 (the daemon reuses the 100's page/lane/action logic, just
driven from telemetry). The RIGHT side adds 5 buttons (creator/audience/link/mute/next-page) + 1
headphone dial; those are the only things in the small `[stream200]` section (a button → an action,
the headset → an audio lane). The new audio channels just appear as PipeWire sources/sinks, so lane
matching handles them with no special code. **The telemetry bit/offset of each physical control is
NOT user config and is NOT guessed** — it lives in `BUTTON_BITS`/`DIAL_FIELDS` in `src/stream200.py`
(one place, code). Partly recovered by time-correlating the full pcap's labeled UAC2 events against
the telemetry: 3 endless main dials = float32 @16/28/80, the headset = a bounded absolute u8 pot
@92, 4 mute bits = byte-14 bits 0–3. The **4 action buttons** (act1..4, "Buttons unter den
Drehknöpfen") were recovered 2026-06-19 from the capture's labeled press order = byte-13 bits 0–3
(byte-aligned: byte14 = push mutes, byte13 = action buttons, byte12 bits0–3 = the 4 main dials
turning). Only the **5 right-side deck buttons** (creator/audience/link/mute/page — they leave no
clear telemetry edge in the capture) and the **4th main dial** stay `None` until pinned on a real
device with `dev/src-re/stream200_probe.py input` (it prints the already-recovered map +
ready-to-paste entries). The config editor's
**200 XLR** tab edits only the right-side actions/lanes; it appears only when a 200 is attached or
already configured (hidden override `[stream200] show_tab = true` forces it on a device-less dev
machine; the daemon ignores it). Audio control works once the table is filled; the **panel display
is a stub** — the op-0x07/op-0x08 image *payload* (pixel/field) format is still being
reverse-engineered, so nothing is synthesized (observe-and-copy replay only, in the probe). The
display *frame builder* exists, though: **`src/sm200.py`** builds the 200's bulk-OUT display frames
from intent (op-0x07 graphic-element chunked upload + op-0x08 image/control frames — the `sm.py`
analogue; **no CRC** on the 200, unlike the 100's SM frames), with the payload bytes left opaque
until the codec is pinned. Its grammar is wire-confirmed and `python3 src/sm200.py` rebuilds every
captured display frame byte-exact. Remaining 200 work is hardware-gated: fill the control map,
decode the display payloads (then feed them through sm200). See `dev/docs/STREAM200-XLR-*.md`.
