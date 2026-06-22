#!/usr/bin/env python3
"""Stream 100 ELEMENT codec (op35 icon / op36 label / op32 gauge sub-blits) — RLE, cracked s6.

Found in hsm_api_core serializer @ ~FUN_180004xxx (decomp_hsm_all.c:2708-2838).
Wire: [ts4][op][b5][b6][datalen:u16][RLE tokens].  b5 = element row (0 dial / 1 button).
RLE token (per run, left->right, top->bottom):
  byte t: run = t>>4, nib = t&0xf
    run == 0  -> fill to END OF ROW with this run's colour
    run 1..15 -> that many pixels
  nib == 0    -> background/transparent run (no colour bytes)
  nib != 0    -> colour run; next 2 bytes = RGB565 (low, mid).  nib = colour bits16-19
                 (kept for exact round-trip; rendering uses the low 16 = 565).
Element dims (from HSM_PrepareGraphicElement): icon op35 = 32x32, label op36 = 110x16.

VERIFIED: real op35 icons decode to recognizable glyphs (purple play-triangle); real op36 labels
decode to readable text ("A","B","AA"); decode->encode round-trips byte-exact on captured elements.
"""
import sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcapdec
from paths import PCAP, RECON

DIMS = {0x35: (32, 32), 0x36: (110, 16)}     # icon, label

def sm(u):
    if len(u) >= 961 and u[952:954] == b"SM":
        ln = struct.unpack_from("<H", u, 954)[0]
        return u[956:956 + ln]
    return None

def rle_decode(data, W, H):
    """-> list[W*H] of 20-bit pixels (bits16-19 = nib, bits0-15 = RGB565; 0 = background)."""
    px = []; col = 0; row = 0; i = 0
    while i < len(data) and row < H:
        t = data[i]; i += 1
        run = t >> 4; nib = t & 0xf
        val = 0
        if nib:
            val = (nib << 16) | (data[i + 1] << 8) | data[i]; i += 2
        n = (W - col) if run == 0 else run
        if col + n > W:
            n = W - col
        px += [val] * n; col += n
        if col >= W:
            col = 0; row += 1
    px += [0] * (W * H - len(px))
    return px[:W * H], i

def rle_encode(px, W, H):
    """inverse: list[W*H] 20-bit pixels -> RLE token bytes (mirrors the device serializer)."""
    out = bytearray()
    for row in range(H):
        col = 0
        base = row * W
        while col < W:
            val = px[base + col]
            j = col + 1
            while j < W and px[base + j] == val:
                j += 1
            runlen = j - col
            while runlen > 0:
                if col + runlen == W:                 # run reaches end of row -> run=0
                    emit = runlen; tok_run = 0
                else:
                    emit = min(runlen, 15); tok_run = emit
                nib = (val >> 16) & 0xf
                out.append((tok_run << 4) | nib)
                if nib:
                    out.append(val & 0xff); out.append((val >> 8) & 0xff)
                col += emit; runlen -= emit
    return bytes(out)

def rgb565(v):
    c = v & 0xffff
    return (((c >> 11) & 0x1f) << 3, ((c >> 5) & 0x3f) << 2, (c & 0x1f) << 3)

def render(px, W, H):
    from PIL import Image
    im = Image.new("RGB", (W, H)); im.putdata([rgb565(v) for v in px]); return im

def elements(path, op):
    for r in pcapdec.out_data_frames(pcapdec.parse(path)):
        if r.ep != 0x01:
            continue
        pl = sm(r.data)
        if pl and len(pl) >= 9 and pl[4] == op:
            dl = struct.unpack_from("<H", pl, 7)[0]
            yield pl, pl[9:9 + dl]

if __name__ == "__main__":
    # round-trip self-test on captured icons + labels
    cases = [(os.path.join(PCAP, "icon-change.pcapng"), 0x35, 32, 32, "icon"),
             (os.path.join(PCAP, "label-change-dial1-button1.pcapng"), 0x36, 110, 16, "label")]
    os.makedirs(RECON, exist_ok=True)
    for path, op, W, H, nm in cases:
        ok = total = 0
        for k, (pl, data) in enumerate(elements(path, op)):
            px, used = rle_decode(data, W, H)
            re = rle_encode(px, W, H)
            total += 1
            if re == data:
                ok += 1
            if k < 3:
                render(px, W, H).resize((W * 4, H * 4)).save(os.path.join(RECON, f"el_{nm}_{k}.png"))
        print(f"{nm} ({op:#x}, {W}x{H}): round-trip {ok}/{total} byte-exact")
