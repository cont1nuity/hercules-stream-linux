"""SM-packet CRC-16 — the gate that decides whether the device parses a frame at all.

FIRMWARE FACT (HSM01_S32L4R7_v1_38, validator FUN_08008efc @ 0x08008efc):
Every "SM" packet carries a CRC-16 at SM bytes [4:6] (= 1904-byte URB offset 956-957).
The device computes the CRC over SM bytes [0:len) (len = SM[2:4]), SKIPPING bytes 4-5
(the CRC field itself), then residue-checks the stored CRC. On mismatch the packet is
dropped (FUN_0800aba4 returns "skip") and FUN_08009930 (the op parser) is NEVER called —
so op40/op31/op30/... are silently ignored.

This is why authored VU frames never rendered: editing the level bytes left the captured
CRC stale, so the device rejected every frame. Verbatim replay only worked because its CRC
still matched. Bytes 956-957 are NOT a timestamp (the old set_ts/Scheduler "ts" was a
misread of this CRC field); SM bytes [6:8] (offset 958-959) ARE a real field the validator
stores, but the op parser starts at SM[8] so it is left as captured here.

Algorithm: table-driven reflected CRC-16, poly 0x8005, init 0 (table matches firmware
table @ flash 0x0803475e exactly: table[1]=0x9705, table[0x80]=0x8005).
"""
import struct

_POLY = 0x8005
_TBL = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ _POLY if (_c & 1) else (_c >> 1)
    _TBL.append(_c & 0xFFFF)
assert _TBL[1] == 0x9705 and _TBL[0x80] == 0x8005, "CRC table mismatch vs firmware"

SM_OFF = 952          # SM packet starts here in the 1904-byte isoc URB ([HERC 952][SM 952])
LEN_OFF = 954         # u16 SM length  (SM[2:4])
CRC_OFF = 956         # u16 CRC-16     (SM[4:6])  <- recompute this after any payload edit


def crc16(sm, length):
    """CRC-16 over SM packet bytes [0:length), skipping bytes 4 and 5 (the CRC field)."""
    crc = 0
    for i in range(length):
        if i == 4 or i == 5:
            continue
        crc = _TBL[(sm[i] ^ crc) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFF


def fix_frame_inplace(buf):
    """Recompute and write the SM CRC into a mutable bytearray. Idempotent on valid frames.
    Returns True if a CRC was written (SM frame), False otherwise (HERC/heartbeat)."""
    if len(buf) < CRC_OFF + 2 or buf[SM_OFF:SM_OFF + 2] != b"SM":
        return False
    ln = struct.unpack_from("<H", buf, LEN_OFF)[0]
    struct.pack_into("<H", buf, CRC_OFF, crc16(memoryview(buf)[SM_OFF:], ln))
    return True


def fix_frame(frame):
    """Return a copy of frame with a correct SM CRC (unchanged if not an SM frame)."""
    b = bytearray(frame)
    fix_frame_inplace(b)
    return bytes(b)


def validate(frame):
    """Mirror of firmware FUN_08008efc for a single-slot SM packet.
    Returns 0=ACCEPT(parse), 1=REJECT(skip), 2=STALL(need more data)."""
    sm = memoryview(frame)[SM_OFF:]
    if not (sm[0] == ord('S') and sm[1] == ord('M')):
        return 1
    ln = struct.unpack_from("<H", frame, LEN_OFF)[0]
    if ln < 9 or ln > 0x2C9F:
        return 1
    if len(sm) < ln:
        return 2
    return 0 if crc16(sm, ln) == struct.unpack_from("<H", frame, CRC_OFF)[0] else 1


if __name__ == "__main__":
    print("CRC table OK (poly 0x8005, init 0): table[1]=0x%04x table[0x80]=0x%04x" %
          (_TBL[1], _TBL[0x80]))
