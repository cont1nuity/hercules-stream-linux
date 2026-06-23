# The daemon

`src/ui.py` — the process `./start.sh` launches. It ties the protocol halves together: knobs and
buttons drive per-app PipeWire volume/mute, the panel shows pages of icons/labels/volume arcs, and
per-lane VU bars meter each lane's own stream. It builds frames via [PROTOCOL.md](PROTOCOL.md) /
[CODECS.md](CODECS.md) and pushes them through the transport in [DEVICE-IO.md](DEVICE-IO.md);
config/log homes, the tray, the lock, and the config editor are in [RUNTIME.md](RUNTIME.md).

## Hotplug supervisor (`run()`)

`run()` renders assets + spawns the tray **once**, then loops: `_wait_for_device` (idle —
everything off: no audio worker / input / metering / lane matching) → `_serve` (one device
session) → idle again on removal, resuming on replug. Any session-fatal exception is isolated so
it can never kill the daemon (the PDEATHSIG'd tray would die with it).

## The slot loop (`_serve`)

Each `_serve` runs one 20 ms slot loop sending **exactly one frame per slot**, by priority:
queued frame > dirty op41/op30 > op40 VU (every 2nd slot) > heartbeat. **Nothing in this loop may
block** — the panel has no framebuffer, so a stalled cadence blanks it. All blocking work lives in
threads:

- **InputReader** — blocking 1 s reads on EP 0x81; decodes via `stream100.decode_events`.
- **AudioWorker** — owns *all* `pactl`. Volume is OS-authoritative: read → clamp (≤100%) → set →
  read back → display. Never changes audio except in response to a knob.
- **PulseEvents** — `pactl subscribe`; kills VU taps bound to removed streams instantly (a dead
  `parec --monitor-stream` tap gets relinked by PipeWire to a sink monitor and would meter
  everything).
- **DeviceWatch** — 1 Hz usbfs presence poll, off the cadence; flags removal.

## VU metering

Per-lane `parec` taps (`src/bars_live.py`, `Meter` / `to_byte`), volume-relative by default, with
instant-attack / hold / fall peak-cap ballistics. Read-only — metering never changes audio. The
op34 color model for the bars is in [PROTOCOL.md](PROTOCOL.md).

## Crash logging

`main()` enables `faulthandler` → `logs/crash.log`, so a C-level abort leaves a per-thread stack
dump. `--debug` enables trace logging.

## Modules

| Module | Role |
|---|---|
| `src/ui.py` | The daemon: hotplug supervisor, slot loop, the four threads, faulthandler. `python3 src/ui.py --selftest` = full offline frame validation. |
| `src/bars_live.py` | Non-blocking per-lane `parec` peak reader (`Meter` / `to_byte`); the live VU source. |

Input decode and the PipeWire helpers live in `src/stream100.py` — see [DEVICE-IO.md](DEVICE-IO.md).
The Stream 200 XLR runs a different daemon path entirely ([STREAM200.md](STREAM200.md)), though it
reuses this daemon's page/lane/action logic.
