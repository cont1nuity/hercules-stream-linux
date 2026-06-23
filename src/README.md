# `src/` module map

All of Hercules Stream for Linux is plain Python in this one flat package — there are no
sub-packages, so each module belongs to a *component* and the deep detail for each component lives
in [`../docs/`](../docs/). Use this table to jump from a file to its doc; see
[../CLAUDE.md](../CLAUDE.md) for the project overview and the non-negotiable rules.

New scripts: `from paths import …` — never hardcode directory locations (`src/paths.py` owns the
dev-vs-packaged config/log switch).

| Module | Component doc | Role |
|---|---|---|
| `sm.py` | [PROTOCOL](../docs/PROTOCOL.md) | Build SM frames from intent (multi-op, multi-packet). |
| `op_walk.py` | [PROTOCOL](../docs/PROTOCOL.md) | Op grammar / frame validator (`walk()`). |
| `vu_crc.py` | [PROTOCOL](../docs/PROTOCOL.md) | The CRC-16 gate (`fix_frame` / `validate`). |
| `wake_data.py` | [PROTOCOL](../docs/PROTOCOL.md) / [DEVICE-IO](../docs/DEVICE-IO.md) | Verbatim panel wake/init frames. |
| `codec_decode.py` | [CODECS](../docs/CODECS.md) | Background image decode (`PHASE` table). |
| `codec_encode.py` | [CODECS](../docs/CODECS.md) | Background image encode. |
| `codec_element.py` | [CODECS](../docs/CODECS.md) | Icon/label/gauge RLE codec (`render`). |
| `element_test.py` | [CODECS](../docs/CODECS.md) | Icon/label/slot-grid selftest. |
| `pcapdec.py` | [CODECS](../docs/CODECS.md) | USBPcap/pcapng parser (capture validation). |
| `display.py` | [DEVICE-IO](../docs/DEVICE-IO.md) | `Scheduler` (cadence, seq+CRC, isoc gate), wake, image. |
| `stream100.py` | [DEVICE-IO](../docs/DEVICE-IO.md) | Input decode + PipeWire helpers. |
| `usbdev.py` | [DEVICE-IO](../docs/DEVICE-IO.md) | `usb.core.find` wrapper (bundled-libusb aware). |
| `devices.py` | [DEVICE-IO](../docs/DEVICE-IO.md) | Variant detection (e053 vs e054). |
| `ui.py` | [DAEMON](../docs/DAEMON.md) | The daemon: hotplug supervisor, slot loop, threads. |
| `bars_live.py` | [DAEMON](../docs/DAEMON.md) | Non-blocking per-lane `parec` VU reader. |
| `stream200.py` | [STREAM200](../docs/STREAM200.md) | Stream 200 XLR transport + telemetry + daemon. |
| `sm200.py` | [STREAM200](../docs/STREAM200.md) | Stream 200 XLR display frame builder (no CRC). |
| `paths.py` | [RUNTIME](../docs/RUNTIME.md) | Single source of truth for config/log dirs. |
| `features.py` | [RUNTIME](../docs/RUNTIME.md) | Build-override + feature-flag resolution. |
| `firstrun.py` | [RUNTIME](../docs/RUNTIME.md) | First-run preflight (host tools, udev, config). |
| `daemonctl.py` | [RUNTIME](../docs/RUNTIME.md) | Find/restart the running daemon. |
| `tray.py` | [RUNTIME](../docs/RUNTIME.md) | System-tray icon (SNI + DBusMenu). |
| `configui.py` | [RUNTIME](../docs/RUNTIME.md) | Tkinter config editor (live preview). |
