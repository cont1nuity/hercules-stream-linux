#!/usr/bin/env python3
"""Stream 100 pixel codec — ENCODER (image -> op38+op37 frames).

Method = OBSERVE & COPY: take REAL op38 + 31 op37 URBs as carriers and overwrite ONLY the
palette + index bytes (and let the replayer advance ts). Nothing else is synthesized.

Frame byte offsets inside the URB `u` (SM payload starts at u[956]):
  op38: palette u[956+27 : 956+27+512] (256xRGB565 LE) ; tile-0 px u[956+548 : +4080]
  op37: tile px  u[956+18 : 956+18+4080]                ; tile index = seq field @ u[956+6]
"""
import sys, os, struct, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcapdec
from codec_decode import FX, FY, LW, LH, PHASE   # tile->phase TABLE (dev/docs/TILE-ORDER-FINDINGS.md)
from paths import PCAP, RECON, TEST_IMAGES

SMO = 956                                   # SM payload offset in the URB
PAL_OFF, PAL_LEN = SMO + 27, 512
T0_OFF = SMO + 548
T37_OFF = SMO + 13                           # op37 pixels start 13 B into SM payload (after datalen)
TILE_PX = 4080
SEQ_OFF = SMO + 6
# Tiles are INTERLACED (polyphase) sub-samples, NOT contiguous blocks: each is a 60x68 downsample
# of the 480x272 frame. Tile t samples at PHASE[t] = the driver's 32-entry Adam7-style table —
# NOT (t%8, t//8); the identity order was the session-9 HW distortion (dev/docs/TILE-ORDER-FINDINGS.md).

def img_to_tiles(idx):
    """row-major 480x272 index buffer -> {tile_index: 4080 px} by INTERLACING (polyphase split)."""
    tiles = {}
    for t in range(32):
        tcol, trow = PHASE[t]
        b = bytearray(TILE_PX)
        for ly in range(LH):
            row = (ly * FY + trow) * 480
            base = ly * LW
            for lx in range(LW):
                b[base + lx] = idx[row + lx * FX + tcol]
        tiles[t] = bytes(b)
    return tiles

def tiles_to_img(tiles):
    """inverse: {tile_index: 4080 px} -> row-major 480x272 index buffer (de-interlace)."""
    screen = bytearray(272 * 480)
    for t, px in tiles.items():
        if not (0 <= t < 32):
            continue
        tcol, trow = PHASE[t]
        for ly in range(LH):
            row = (ly * FY + trow) * 480
            base = ly * LW
            for lx in range(LW):
                screen[row + lx * FX + tcol] = px[base + lx]
    return bytes(screen)

def sm_op(u):
    if len(u) >= 961 and u[952:954] == b"SM":
        return u[960]                       # pl[4]
    return None

def load_carriers(cap):
    """Return (op38_urb, {seq: op37_urb}) for the first clean op38 + seq 1..31 run."""
    op38 = None; tiles = {}
    for r in pcapdec.out_data_frames(pcapdec.parse(cap)):
        if r.ep != 0x01:
            continue
        u = r.data; op = sm_op(u)
        if op == 0x38 and len(u) >= T0_OFF + TILE_PX:
            if op38 is not None and len(tiles) >= 31:
                break
            op38 = bytearray(u); tiles = {}
        elif op == 0x37 and op38 is not None and len(u) >= T37_OFF + TILE_PX:
            seq = struct.unpack_from("<H", u, SEQ_OFF)[0]
            if 1 <= seq <= 31 and seq not in tiles:
                tiles[seq] = bytearray(u)
    return op38, tiles

def quantize(img_path):
    """-> (palette: list[256] u16 RGB565, indices: bytes[130560] row-major)."""
    from PIL import Image
    im = Image.open(img_path).convert("RGB").resize((480, 272))
    # reduce to the RGB565 grid first (match the device's r5g6b5), then adaptive-quantize to <=256
    px = bytearray(im.tobytes())
    for i in range(0, len(px), 3):
        px[i]   = px[i]   & 0xf8           # R 5 bits
        px[i+1] = px[i+1] & 0xfc           # G 6 bits
        px[i+2] = px[i+2] & 0xf8           # B 5 bits
    im565 = Image.frombytes("RGB", (480, 272), bytes(px))
    pim = im565.quantize(colors=256, method=Image.MEDIANCUT)
    rgb = pim.getpalette()                 # flat RGB triples
    rgb = rgb + [0] * (768 - len(rgb))     # pad to 256 entries
    pal = []
    for k in range(256):
        r, g, b = rgb[3*k], rgb[3*k+1], rgb[3*k+2]
        pal.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return pal, pim.tobytes()

def encode(img_path, cap):
    op38, tiles = load_carriers(cap)
    if op38 is None or len(tiles) < 31:
        raise SystemExit(f"need a clean 32-tile carrier screen; got op38={op38 is not None} tiles={len(tiles)}")
    pal, idx = quantize(img_path)
    tmap = img_to_tiles(idx)                                # grid split (NOT linear)
    palbytes = struct.pack("<256H", *pal)
    op38[PAL_OFF:PAL_OFF + PAL_LEN] = palbytes
    op38[T0_OFF:T0_OFF + TILE_PX] = tmap[0]                 # tile 0 = top-left cell
    out = [bytes(op38)]
    for seq in range(1, 32):
        u = tiles[seq]
        u[T37_OFF:T37_OFF + TILE_PX] = tmap[seq]
        out.append(bytes(u))
    return out, pal, idx

# ---- self-test: round-trip encode -> decode -> compare ----
def _decode_urbs(urbs):
    pal = None; tiles = {}
    for u in urbs:
        op = sm_op(u)
        if op == 0x38:
            pal = struct.unpack_from("<256H", u, PAL_OFF)
            tiles[0] = u[T0_OFF:T0_OFF + TILE_PX]
        elif op == 0x37:
            seq = struct.unpack_from("<H", u, SEQ_OFF)[0]
            tiles[seq] = u[T37_OFF:T37_OFF + TILE_PX]
    return pal, tiles_to_img(tiles)        # grid reassembly -> row-major

if __name__ == "__main__":
    img = sys.argv[1] if len(sys.argv) > 1 else os.path.join(TEST_IMAGES, "markers.png")
    cap = sys.argv[2] if len(sys.argv) > 2 else os.path.join(PCAP, "background-color-change-alphabetical.pcapng")
    urbs, pal, idx = encode(img, cap)
    print(f"encoded '{img}' -> {len(urbs)} frames (1 op38 + {len(urbs)-1} op37), palette colors={len(set(pal))}")
    # round-trip
    rpal, rbuf = _decode_urbs(urbs)
    same_pal = tuple(pal) == tuple(rpal)
    same_idx = rbuf == idx
    print(f"round-trip: palette match={same_pal}  index match={same_idx}")
    # render the decoded result to PNG so it can be eyeballed
    from PIL import Image
    def c(v): return (((v>>11)&0x1f)<<3, ((v>>5)&0x3f)<<2, (v&0x1f)<<3)
    out = Image.new("RGB", (480, 272)); out.putdata([c(rpal[b]) for b in rbuf])
    out.save(os.path.join(RECON, "encoded_roundtrip.png"))
    # also save the quantized source for visual diff
    src = Image.new("RGB", (480, 272)); src.putdata([c(pal[b]) for b in idx])
    src.save(os.path.join(RECON, "encoded_source.png"))
    print("wrote recon/encoded_source.png (target) and recon/encoded_roundtrip.png (decoded back)")
