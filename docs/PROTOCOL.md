# Display wire format

The host→panel protocol for the Stream 100's 4.3" 480×272 LCD: how a logical frame is framed on
the isochronous stream, the CRC-16 gate the firmware enforces, and the op grammar every frame is
built from. **This is the load-bearing reverse-engineered knowledge** — treat every byte here as
decoded-and-verified or a capture constant, never guess (see the non-negotiable rules in
[../CLAUDE.md](../CLAUDE.md)).

The USB transport that carries these frames (endpoints, the 20 ms `Scheduler`, the isoc removal
gate) is in [DEVICE-IO.md](DEVICE-IO.md); the pixel codecs behind the image/element ops (0x35–0x38)
are in [CODECS.md](CODECS.md).

## Frame structure

A logical frame on the isoc stream = a `"HERCULES"`+pad sync packet, then 1..N 952-byte payload
packets (firmware length cap 0x2C9F):

    "SM" | len:u16 LE | CRC16 | seq:u16 | ops…        (len = 8 + len(ops))

- **CRC-16 is a hard gate**: reflected, poly 0x8005, init 0, over SM[0:len) skipping bytes 4–5
  (the CRC field itself). Firmware drops bad-CRC frames *silently* before parsing. `src/vu_crc.py`
  implements it; `Scheduler.send()` (see [DEVICE-IO.md](DEVICE-IO.md)) applies it on every send.
  ⚠️ This field was historically misread as a timestamp — it is not; never stamp a time there.
- **`seq`** is a per-send sequence counter, stamped by `Scheduler.send()`.
- **Frames are multi-op bundles.** `src/op_walk.py` is the authoritative grammar for every op
  (validated byte-exact against 9,946 captured frames). Don't hand-parse offsets — use
  `op_walk.walk()`.
- **No framebuffer.** The panel blanks the moment the ~20 ms heartbeat cadence stops; heartbeats
  alone don't repaint it.

## The ops

| Op | Name | Payload / meaning |
|---|---|---|
| **0x00** | heartbeat | keeps the panel alive (drives the 20 ms cadence) |
| **0x30** | channel LED | `[ch][state]` — 0 off / 1 on / 2 blink |
| **0x31** | brightness | `0..100` (0 = panel off) |
| **0x32** | meter/page config | partially decoded — capture constants |
| **0x33** | palette upload | — |
| **0x34** | per-slot meter config | `[sel][a,b,c:u16][cnt][cnt×RGB565]` — color model below |
| **0x35** | icon 32×32 | RLE bitmap, addressed `[row][column-slot]` (see [CODECS.md](CODECS.md)) |
| **0x36** | label 110×16 | RLE bitmap, addressed `[row][column-slot]` (see [CODECS.md](CODECS.md)) |
| **0x37/0x38** | background image | 256-color palette + 32 interlaced tiles (see [CODECS.md](CODECS.md)) |
| **0x40** | VU bar levels | per-lane bar levels |
| **0x41** | volume display | `[id][val:u16][val2:u16]` (percent + arc) |

**op34 color model** (hardware-verified 2026-06-12): `colors[cnt≤16]` = bar body; `cnt≥2` = a
vertical gradient the firmware interpolates (stops bottom→top); `b` = clip-zone color (the top 16
of 121 levels ≈ 13% of the bar, size firmware-fixed); `c` = peak-cap color (firmware fades its
trail); `a` = bar background. The config keys that drive it are in [STATUS.md](STATUS.md).

**Element addressing.** op35/op36 bitmaps are placed by `[row][column-slot]` into a fixed 2×4
grid — **no X/Y coordinates exist on the wire**; screen positions are hardcoded in firmware.

## Two planes

op37/op38 own only the background; firmware composites the element layers (icons/labels, VU bars,
button row) on top — a background repaint doesn't touch them.

## Modules

| Module | Role |
|---|---|
| `src/sm.py` | Builds SM frames from intent (`heartbeat`/`brightness`/`vu`/`generic`, multi-packet `pack_sm`). `python3 src/sm.py` = byte-match acceptance tests. |
| `src/op_walk.py` | Op grammar / frame validator (`walk()`); authoritative, byte-exact vs 9,946 captured frames. |
| `src/vu_crc.py` | The CRC-16 (`fix_frame`, `validate`). `python3 src/vu_crc.py` = self-test. |
| `src/wake_data.py` | The 32 verbatim panel wake/init frames (capture constants), replayed by `display.wake` (see [DEVICE-IO.md](DEVICE-IO.md)). |

Offline selftests for this layer: `python3 src/sm.py`, `python3 src/vu_crc.py` (and the full
daemon validation `python3 src/ui.py --selftest`).
