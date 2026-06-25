#!/usr/bin/env python3
"""SM ops-region walker — the complete op grammar from the firmware parser FUN_08009930
(artifacts/fw-op40.c:2166). An SM frame's ops region (@960) is a SEQUENCE of records; op 0x00
terminates the frame (heartbeat = a bare 0x00). Validated by walking every captured frame.

Record formats (payload bytes AFTER the opcode):
  00            terminator / heartbeat (rest of frame = zero padding)
  10            23B session/config blob
  11            1B    12/13  4B session start/end    14,15,16,17  0B
  20            1B enable
  21            [cnt][sum16][type] + min(cnt,8) data  (echo channel, unseen in captures)
  22            2B
  30            [ch][state]   ch<4; state 0=off 1=on 2=on+alt -> per-channel state bits
  31            [flags][x] + 1B value if flags&1      brightness (0..100)
  39            [flags][x] + 1B value if flags&1      second slider (stored +0x54)
  32            [mode] + (mode==0 ? 4ch x 6B channel-config : 16B skipped)
                6B/ch: observed 84 01 07 07 00 64 ; byte2&1 / byte3&1 gate dial/button VU
  33            [a][b][offset][count:u16][f] + count x u16 -> LUT/palette upload @a188
  34            [sel][a:u16][b:u16][c:u16][count:u8] + count x u16 (<=16)
                slot=(sel&3)+(sel>>7)*4 -> 8 slots; per-slot meter config, dirty|=4 (VU layer)
  35            [row][slot][datalen:u16] + RLE until 32 rows  icon -> buf(row) + (slot&3)*0xc00
  36            [row][slot][datalen:u16] + RLE until 16 rows  label -> buf(row) + slot*0x14a0
  37            [mode=1][tile:u32(&0x1f)][skip][datalen:u16] + 4080B RAW (68x60, 1B/px)
  38            15B screen-start header (palette follows as an op33 record: offset 0 count 256)
  40            [ch|flags] + (flags&0x40 ? 4 x u16 : 4 x u8)  VU levels (bit7 = mirror bank)
  41            [id][val:u16][val2:u16]  volume display: percent=val*100/0xffff + arc pos;
                id&3=ch, bit7=bank, bits5/6=grey/mute state. (NOT a glow animation.)
  50            1B (bit7 -> page flag, bit6 -> FUN_0800cacc)

RLE token (op35/36): t=run<<4|nib; nib!=0 -> +2B RGB565; run==0 -> fill to end of row.
"""
import sys, os, struct, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcapdec
from paths import PCAP

FIXED = {0x10: 23, 0x11: 1, 0x12: 4, 0x13: 4, 0x14: 0, 0x15: 0, 0x16: 0, 0x17: 0,
         0x20: 1, 0x22: 2, 0x30: 2, 0x38: 15, 0x50: 1}

ROWS = {0x35: (32, 32), 0x36: (16, 110)}     # rows, width


def walk(ops):
    """Yield (offset, op, payload_bytes) records; raise ValueError on unknown op."""
    i = 0
    while i < len(ops):
        op = ops[i]; start = i; i += 1
        if op == 0x00:
            if any(ops[i:]):
                # firmware stops at 0x00; non-zero tail would be silently dropped
                raise ValueError(f"non-zero bytes after terminator @ {i}: {ops[i:i+8].hex()}")
            yield (start, op, b"")
            return
        if op in FIXED:
            n = FIXED[op]
        elif op in (0x31, 0x39):
            n = 2 + (1 if ops[i] & 1 else 0)
        elif op == 0x32:
            n = 1 + (24 if ops[i] == 0 else 16)
        elif op == 0x21:
            n = 4 + min(ops[i], 8)
        elif op == 0x33:
            n = 6 + struct.unpack_from("<H", ops, i + 3)[0] * 2
        elif op == 0x34:
            n = 8 + ops[i + 7] * 2
        elif op in ROWS:
            # advance past the RLE pixel data exactly like the firmware: count rows, stop after nrows
            nrows, width = ROWS[op]
            j = i + 4; row = col = 0
            while row < nrows and j < len(ops):
                t = ops[j]; j += 1
                if t & 0x0f:
                    j += 2
                col = width if (t & 0xf0) == 0 else col + (t >> 4)
                if col >= width:
                    row += 1; col = 0
            n = j - i
        elif op == 0x37:
            n = 8 + 4080
        elif op == 0x40:
            n = 1 + (8 if ops[i] & 0x40 else 4)
        elif op == 0x41:
            n = 5
        else:
            raise ValueError(f"unknown op {op:#04x} @ {start}")
        yield (start, op, bytes(ops[i:i + n]))
        i += n


def sm_ops(u):
    """URB bytes -> ops region of the SM frame (None if not an SM URB)."""
    if len(u) >= 961 and u[952:954] == b"SM":
        ln = struct.unpack_from("<H", u, 954)[0]
        return u[960:952 + ln]
    return None


def main():
    names = sys.argv[1:] or sorted(f for f in os.listdir(PCAP) if f.endswith(".pcapng"))
    grand = collections.Counter()
    for name in names:
        path = os.path.join(str(PCAP), name)
        hist = collections.Counter(); bad = 0; frames = 0
        for r in pcapdec.out_data_frames(pcapdec.parse(path)):
            if r.ep != 0x01:
                continue
            ops = sm_ops(bytes(r.data))
            if ops is None:
                continue
            frames += 1
            try:
                for _, op, _pl in walk(ops):
                    hist[op] += 1
            except (ValueError, IndexError, struct.error) as e:
                bad += 1
                if bad <= 3:
                    print(f"  PARSE FAIL {name} frame {frames}: {e}")
        grand.update(hist)
        ops_s = " ".join(f"{k:02x}:{v}" for k, v in sorted(hist.items()))
        print(f"{name}: frames={frames} bad={bad}  ops {ops_s}")
    print("\nTOTAL:", " ".join(f"{k:02x}:{v}" for k, v in sorted(grand.items())))


if __name__ == "__main__":
    main()
