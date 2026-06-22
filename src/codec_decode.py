#!/usr/bin/env python3
"""Stream 100 pixel codec — DECODER (verified session 4).

Every graphic element is palette-indexed RGB565: an inline palette + 1-byte/px indices.
  - Background (480x272): op38 = screen start = [ts4][38][hdr] + 256xRGB565 palette @ pl[27:539]
                                                + tile-0 pixels @ pl[548:4628];
                          op37 = tiles 1..31  = [hdr18] + 4080 index bytes  (pl[18:4098]).
  - Gauge (op32): 4x dial-state [84 01][state][69 64] + embedded blit (small inline palette).
  - Icon op35 / label op36: header + inline palette + indices.

Run: python3 codec_decode.py            # render all backgrounds in the bg capture -> recon/*.png
     python3 codec_decode.py <cap.pcapng>
Reverse (image -> frames) is the encoder, next step; this proves the decode by re-rendering.
"""
import sys, struct, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcapdec
from paths import PCAP, RECON

PAL_OFF, PAL_LEN = 27, 512          # op38: 256 x RGB565 LE
T0_OFF = 548                         # op38: tile-0 pixels
TILE_PX = 4080
# The 32 op37/op38 "tiles" are INTERLACED (polyphase) sub-samples of the 480x272 frame, NOT
# contiguous blocks: each tile is a 60x68 (=480/8 x 272/4) downsample of the whole screen, stored
# row-major. Tile t's phase comes from a 32-entry TABLE (DAT_180107b30 in hsm_api_core_x64.dll;
# driver gather FUN_1800156d0: tile_t[r][c] = fb[rowphase(t)+4r][colphase(t)+8c]) in Adam7-style
# progressive order — NOT (t%8, t//8). The identity-order assumption was the session-9 HW
# distortion: every pixel landed in its correct 8x4 cell but at the wrong sub-position
# (dev/docs/TILE-ORDER-FINDINGS.md; round-trips never caught it because phase permutations cancel).
FX, FY, LW, LH = 8, 4, 60, 68       # horiz phases, vert phases, local tile width, height
# PHASE[t] = (colphase, rowphase); table entries also carry repX/repY (progressive pixel-
# replication factors used by the device until finer tiles arrive) which we don't need here.
PHASE = [
    (0, 0), (4, 0), (0, 2), (4, 2), (2, 0), (6, 0), (2, 2), (6, 2),
    (0, 1), (2, 1), (4, 1), (6, 1), (0, 3), (2, 3), (4, 3), (6, 3),
    (1, 0), (3, 0), (5, 0), (7, 0), (1, 2), (3, 2), (5, 2), (7, 2),
    (1, 1), (3, 1), (5, 1), (7, 1), (1, 3), (3, 3), (5, 3), (7, 3),
]
assert sorted(PHASE) == [(c, r) for c in range(FX) for r in range(FY)]  # exact permutation

def sm(u):
    if len(u) >= 961 and u[952:954] == b"SM":
        ln = struct.unpack_from("<H", u, 954)[0]
        return u[956:956 + ln]
    return None

def frames(path):
    for r in pcapdec.out_data_frames(pcapdec.parse(path)):
        if r.ep == 0x01:
            pl = sm(r.data)
            if pl and len(pl) >= 5:
                yield pl

def rgb565_to_888(v):
    return (((v >> 11) & 0x1f) << 3, ((v >> 5) & 0x3f) << 2, (v & 0x1f) << 3)

def screens(path):
    """Yield dict(pal=bytes512, tiles={seq:bytes4080}) per background repaint."""
    cur = None
    for pl in frames(path):
        op = pl[4]
        if op == 0x38 and len(pl) == 4633:
            if cur:
                yield cur
            cur = {"pal": pl[PAL_OFF:PAL_OFF + PAL_LEN], "tiles": {0: pl[T0_OFF:T0_OFF + TILE_PX]}}
        elif op == 0x37 and len(pl) == 4098 and cur is not None:
            seq = struct.unpack_from("<H", pl, 6)[0]
            # op37 header = [ts4][37][b5][seq:u16][3B][datalen:u16] = 13 B; pixels start at pl[13]
            # (NOT pl[18] — the old +5 skip read 5 phantom header bytes and rotated every tile).
            cur["tiles"][seq] = pl[13:13 + TILE_PX]
    if cur:
        yield cur

def assemble(tiles):
    """{tile_index: 4080 px} -> row-major 480x272 index buffer, by DE-INTERLACING the polyphase
    tiles: tile t's pixel (lx,ly) maps to screen (lx*FX + PHASE[t].col, ly*FY + PHASE[t].row)."""
    screen = bytearray(272 * 480)
    present = set()
    for t, px in tiles.items():
        if not (0 <= t < 32):
            continue
        present.add(t)
        tcol, trow = PHASE[t]
        for ly in range(LH):
            row = (ly * FY + trow) * 480
            base = ly * LW
            for lx in range(LW):
                screen[row + lx * FX + tcol] = px[base + lx]
    # Lossy isoc captures drop whole tiles -> that phase (every FX-th col / FY-th row) reads as a
    # regular dot grid. Fill each hole from the nearest present neighbour column so recons look
    # clean. (Decode-only cosmetics; our encoder always emits all 32 tiles.)
    holes = {PHASE[t] for t in range(32) if t not in present}
    for tcol, trow in holes:
        for y in range(trow, 272, FY):
            base = y * 480
            for x in range(tcol, 480, FX):
                src = x - 1 if x > 0 else x + 1
                while (src % FX, y % FY) in holes and 0 < src < 479:
                    src += -1 if src < x else 1
                screen[base + x] = screen[base + src]
    return bytes(screen)

def render(sc):
    """-> (PIL.Image 480x272, ntiles, ncolors).  Missing tiles (isoc loss) -> index 0."""
    from PIL import Image
    pal = struct.unpack("<256H", sc["pal"])
    buf = assemble(sc["tiles"])
    img = Image.new("RGB", (480, 272))
    img.putdata([rgb565_to_888(pal[i]) for i in buf])
    return img, len(sc["tiles"]), len(set(buf))

if __name__ == "__main__":
    cap = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PCAP, "background-color-change-alphabetical.pcapng")
    out = RECON
    os.makedirs(out, exist_ok=True)
    n = 0
    for k, sc in enumerate(screens(cap)):
        img, nt, nc = render(sc)
        p = f"{out}/bg_{k:02d}_{nt}tiles_{nc}colors.png"
        img.save(p)
        n += 1
        print(f"{p}")
    print(f"rendered {n} screens -> {out}/")
