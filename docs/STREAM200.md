# Stream 200 XLR (experimental)

A second hardware variant (USB `06f8:e054`) wired in so testers can run it. **EXPERIMENTAL — not
yet hardware-verified; gated OFF by default** behind the `stream200` feature flag
([RUNTIME.md](RUNTIME.md) → "Build overrides & feature flags"). A stock build refuses a detected
200 with a hint to enable it; turn it on per-user (`[features] stream200 = true` in `config.toml`)
or per-build (`packaging/build-appimage.sh --enable-stream200`).

The 200 does **not** use the 100's isoc/SM/CRC stack — it has its own bulk-IF3 transport and
op-0x01 telemetry poll (decoded / capture-constant only). `src/devices.py` detects the variant and
`ui.py` / `firstrun.py` dispatch a 200 to `src/stream200.py`.

## Control model

- **MAIN area is identical to the 100** — 4 dials (each push-to-mute) + 4 action buttons,
  configured per page via `[[pages]]` exactly like the 100. The daemon reuses the 100's
  page/lane/action logic, just driven from telemetry (see [DAEMON.md](DAEMON.md)).
- **RIGHT side** adds 5 buttons (creator / audience / link / mute / next-page) + 1 headphone dial
  — the only things in the small `[stream200]` config section (a button → an action, the headset →
  an audio lane). The new audio channels just appear as PipeWire sources/sinks, so lane matching
  handles them with no special code.
- The config editor's **200 XLR** tab edits only the right-side actions/lanes; it appears only when
  a 200 is attached or already configured (hidden override `[stream200] show_tab = true` forces it
  on a device-less dev machine; the daemon ignores it).

## Telemetry map (`BUTTON_BITS` / `DIAL_FIELDS` — code, not user config, not guessed)

Each physical control's telemetry bit/offset lives in `src/stream200.py`, recovered by
time-correlating the full pcap's labeled UAC2 events against the telemetry:

- 3 endless main dials = float32 @16/28/80; headset = a bounded absolute u8 pot @92.
- byte-14 bits 0–3 = the 4 push-to-mute; byte-13 bits 0–3 = the 4 action buttons (act1..4,
  recovered 2026-06-19 from the capture's labeled press order); byte-12 bits 0–3 = the 4 main dials
  turning.
- Still `None` until pinned on a real device: the **5 right-side deck buttons**
  (creator / audience / link / mute / page — they leave no clear telemetry edge in the capture) and
  the **4th main dial**. `dev/src-re/stream200_probe.py input` prints the recovered map plus
  ready-to-paste entries.

Audio control (knobs/buttons → PipeWire) works once the table is filled.

## Display (stub)

The **panel display is a stub** — the op-0x07/op-0x08 image *payload* (pixel/field) format is still
being reverse-engineered, so nothing is synthesized (observe-and-copy replay only, in the probe).
The frame *builder* exists, though: `src/sm200.py` builds the 200's bulk-OUT display frames from
intent (op-0x07 graphic-element chunked upload + op-0x08 image/control frames — the `sm.py`
analogue; **no CRC** on the 200, unlike the 100's SM frames), with the payload bytes left opaque
until the codec is pinned. Its grammar is wire-confirmed and `python3 src/sm200.py` rebuilds every
captured display frame byte-exact.

## Modules

| Module | Role |
|---|---|
| `src/stream200.py` | Bulk-IF3 transport + op-0x01 telemetry decode + control map (`BUTTON_BITS` / `DIAL_FIELDS`) + the 200 daemon (`run`). `python3 src/stream200.py selftest` (offline); `info` / `run -c config.toml`. |
| `src/sm200.py` | Builds the 200's bulk-OUT display frames from intent (no CRC). `python3 src/sm200.py` = byte-match acceptance tests. |

Remaining 200 work is hardware-gated: fill the control map, decode the display payloads (then feed
them through `sm200`). See `dev/docs/STREAM200-XLR-*.md` (the maintainer's private RE repo) and the
status line in [STATUS.md](STATUS.md).
