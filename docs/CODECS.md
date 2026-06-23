# Image & element codecs

The pixel encoders/decoders behind the panel's image and element ops: the background image codec
(ops 0x37/0x38) and the icon/label/gauge RLE codec (ops 0x35/0x36). The op *grammar* these feed
lives in [PROTOCOL.md](PROTOCOL.md); this file is the pixel-format detail.

## Background image (op 0x37 / 0x38)

256-color palette + 32 tiles. Each tile is an *interlaced polyphase downsample* of the 480×272
frame in an Adam7-style progressive order — the `PHASE` table in `src/codec_decode.py` defines
the order. op37/op38 own only the background plane; firmware composites the element layers on top
(see "Two planes" in [PROTOCOL.md](PROTOCOL.md)).

## Icons & labels (op 0x35 / 0x36)

RLE bitmaps: icon 32×32 (op35), label 110×16 (op36), addressed by `[row][column-slot]` into a
fixed 2×4 grid (no wire coordinates — see [PROTOCOL.md](PROTOCOL.md)). `src/codec_element.py` also
renders the volume gauge, and is the render path the config editor's live preview reuses (see
[RUNTIME.md](RUNTIME.md)).

## Modules

| Module | Role |
|---|---|
| `src/codec_decode.py` | Background image decode (palette + tile interlace; owns the `PHASE` table). |
| `src/codec_encode.py` | Background image encode (the inverse). |
| `src/codec_element.py` | Icon/label/gauge RLE codec (`render`). |
| `src/element_test.py` | `--selftest` builds icon/label/slot-grid frames and validates them. |
| `src/pcapdec.py` | Dependency-free USBPcap/pcapng parser; the codecs, `op_walk`, `display`, and the offline selftests import it to validate against captured frames. |

Offline selftest for this layer: `python3 src/element_test.py --selftest`.
