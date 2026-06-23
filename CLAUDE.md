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
python3 src/tray.py --selftest     # tray: update version-compare + [ui] config-key writer (no dbus/net)

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

## Documentation

CLAUDE.md is the hub: the overview, the non-negotiable rules, and how to run things. The detail
for each component lives in `docs/`, one doc per component; `src/README.md` maps every module to
its doc. Read the doc for the part you're touching — don't work the wire format from memory.

| Doc | Covers |
|---|---|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | **Display wire format** — SM framing, the CRC-16 gate, the op grammar (0x00–0x41), two planes. The load-bearing RE knowledge. |
| [docs/CODECS.md](docs/CODECS.md) | Background image codec (op37/38, the `PHASE` interlace) + icon/label/gauge RLE codec (op35/36). |
| [docs/DEVICE-IO.md](docs/DEVICE-IO.md) | USB transport (interfaces/endpoints), the `Scheduler` (cadence, seq+CRC, isoc removal gate), input report decode, variant detection. |
| [docs/DAEMON.md](docs/DAEMON.md) | The daemon (`ui.py`) — hotplug supervisor, the 20 ms slot loop, the four worker threads, VU metering, the audio control model. |
| [docs/STREAM200.md](docs/STREAM200.md) | The experimental Stream 200 XLR variant — its own transport/telemetry, control map, display stub. |
| [docs/RUNTIME.md](docs/RUNTIME.md) | Config/log homes, the AppImage build, build overrides & feature flags, first-run preflight, the single-instance lock, the tray, the config editor. |
| [docs/STATUS.md](docs/STATUS.md) | Implemented, hardware-verified state — one line per shipped feature. |
| [src/README.md](src/README.md) | Module map — every `src/*.py` → its component doc. |

**Cross-cutting essentials** (detail in [docs/RUNTIME.md](docs/RUNTIME.md)): never hardcode paths
— import `src/paths.py` (it owns the dev-vs-packaged config/log switch, `config_path()` / `LOGS`);
settings/feature-flags resolve as `build_overrides.toml` > `config.toml` > code defaults
(`src/features.py`; the `stream200` backend is gated OFF by default); the daemon holds an exclusive
flock on `$XDG_RUNTIME_DIR/hercules-stream.lock` (one instance owns the device; `src/daemonctl.py`
uses it to find/restart the daemon); in the AppImage the bundled libusb is pinned via
`HERCULES_STREAM_LIBUSB`, **not** `LD_LIBRARY_PATH`, so spawned `pactl`/`parec` keep resolving host
libraries; the tray and config editor are separate processes (the 20 ms slot loop must never host
an event loop).

### Repo layout
- `src/` — all Python (flat package). `src/README.md` maps each module to its component doc;
  `src/paths.py` is the single source of truth for directory locations (import from it, never
  hardcode; scripts run from any cwd).
- `icons/` — original, redistributable 18-icon set (`icons/README.md` documents naming/formats).
- `fonts/` — Noto Sans for label rendering (SIL OFL 1.1, see `fonts/OFL.txt`).
- `logs/` — daemon logs, one file per session (`ui-YYYYmmdd-HHMMSS.log`, 10 kept).
- `packaging/` — `build-appimage.sh` (see [docs/RUNTIME.md](docs/RUNTIME.md)). `build/` +
  `dist/` are its scratch/output (never commit them).
- `docs/` — the component reference docs (see the Documentation table above), read on demand.
- `start.sh` / `setup.sh` — entry point / installer. License: GPL-3.0-or-later (`LICENSE`).
- `dev/` — if present: the maintainer's private RE repo (see note at top), ignored by git here.

There is no hardware-independent test suite. The selftests above validate every generated frame
offline (CRC gate + op grammar); **anything visual is verified by a photo of the panel** — the
device is the only renderer that counts. If you change frame-building code, run the selftests;
if you change what's drawn, test on hardware.

## Status

v1 is feature-complete and hardware-verified (input, pages, volume/mute, per-lane VU, colors,
icons); the graphical config editor and AppImage packaging are done, and **v1.0.0 is published on
GitHub Releases** (CI runs the offline selftests on every push; a `v*` tag builds the AppImage and
publishes a Release with notes from `CHANGELOG.md`). Post-v1.0.0: device-less tray-idle +
bidirectional hotplug (the prior libusb isoc-`abort()` on removal is fixed via the usbfs-node
gate), a greyed tray icon while idle, and `faulthandler` crash logging. Full per-feature status is
in [docs/STATUS.md](docs/STATUS.md); the experimental Stream 200 XLR work is in
[docs/STREAM200.md](docs/STREAM200.md).

When you verify or disprove something on hardware, record it in [docs/STATUS.md](docs/STATUS.md);
if it's load-bearing wire-format, update [docs/PROTOCOL.md](docs/PROTOCOL.md) too.
