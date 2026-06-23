# Device I/O (USB transport & input)

The USB layer that moves frames to the panel and events from the controls: the interface/endpoint
map, the `Scheduler` that owns the 20 ms send cadence (sequence + CRC + the isoc removal gate),
panel wake, and the input-report decoder. The frame format on the wire is in
[PROTOCOL.md](PROTOCOL.md); who drives this transport (the daemon, its threads, the audio side) is
in [DAEMON.md](DAEMON.md).

## Transport (verified)

- **Interface 0** (vendor 0xFF): EP `0x02` bulk OUT = host→device commands; EP `0x81` interrupt
  IN = input events. No kernel driver binds → libusb claims directly.
- **Interface 1** alt 1: EP `0x01` isochronous OUT, **952-byte packets** = the LCD stream.

## Scheduler (`src/display.py`)

`Scheduler` owns the 20 ms send cadence. It stamps `seq` and recomputes the CRC-16 on **every**
send — the single chokepoint, so route all device traffic through it — then writes the frame to
the isoc endpoint. It also handles panel wake (replaying the verbatim frames in
`src/wake_data.py`) and image display.

**Isoc removal gate (non-negotiable).** An isochronous write to a surprise-removed device makes
pyusb pass a negative iso-packet count to `libusb_alloc_transfer` → `SIGABRT` (uncatchable — no
Python `except` can stop it). `Scheduler.send()` gates every isoc write on the device's usbfs node
still existing and stops cleanly (sets `device_gone`) when it vanishes — never issue an isoc write
without that guard.

## Input report (`src/stream100.py`)

64-byte report: encoders = signed-16 absolute counters at bytes 3/5/7/9; buttons = bitmask at
byte 1 (low nibble = encoder pushes, high nibble = action buttons). `decode_events()` decodes it.
`stream100.py` also holds the PipeWire helpers the daemon uses for the audio side — see
[DAEMON.md](DAEMON.md).

## Device detection (`src/devices.py`)

Detects the variant by USB id — Stream 100 (`06f8:e053`) vs Stream 200 XLR (`06f8:e054`) — so
`ui.py` / `firstrun.py` dispatch to the right backend ([STREAM200.md](STREAM200.md) for the 200,
which uses a different transport entirely).

## Modules

| Module | Role |
|---|---|
| `src/display.py` | `Scheduler` (cadence, seq+CRC on send, isoc usbfs gate / `device_gone`), panel wake, image display. Manual tests: `python3 src/display.py [--secs N]` / `--image x.png`. |
| `src/stream100.py` | Input decode (`decode_events`) + PipeWire helpers (`sink_inputs` resolves app names via the client object for property-less streams, e.g. Spotify). CLI: `info` / `probe` / `list` / `run`. |
| `src/usbdev.py` | `usb.core.find` wrapper; honors `HERCULES_STREAM_LIBUSB` (AppImage bundled libusb). All daemon device-opens go through it. |
| `src/devices.py` | Variant detection (e053 vs e054). |
| `src/wake_data.py` | The 32 verbatim wake/init frames replayed by `display.wake` (also listed under [PROTOCOL.md](PROTOCOL.md)). |
