#!/usr/bin/env python3
"""SM package builder — construct Stream 100 wire frames from INTENT, no captured carrier needed.

Wire format (fully known since SESSION 8, verified byte-exact vs captures in __main__ below):
  logical frame = 1904-byte isoc URB on EP 0x01 = HERC(952) + SM(952)
    HERC = b"HERCULES" + zero padding to 952  (constant, device-bound keepalive half)
    SM   : off0 "SM" | off2 len:u16 | off4 CRC16:u16 | off6 seq:u16 | off8 ops... | zero pad -> 952
  len   = 8 + len(ops)  (byte count the CRC covers, from SM[0])
  CRC16 = reflected poly 0x8005 init 0 over SM[0:len) skipping bytes 4-5 (vu_crc.py); the device
          validator (FUN_08008efc) drops any frame whose CRC is stale BEFORE the op parser runs.
  seq   = u16, +1 on every SM frame (real HW; baseline capture starts at 1). The op parser does not
          hard-gate on it — the transport (display.Scheduler) owns and stamps it, so builders here
          emit seq=0.

Op payloads (verified against captures, see __main__ acceptance tests):
  heartbeat : ops = 00                                   (len  9; ~50 Hz scaffold between real ops)
  brightness: ops = 31 01 00 VV 00, VV = 0..100          (len 13)
  vu        : ops = [40 blk curL curR pkL pkR] + identical mirror block with blk|0x80, per channel,
              then a single 00 terminator               (len 21 one channel, 33 two, ...)
              blk 0x00=master 0x01=mic 0x02=playback; invariant pk >= cur per side.

The heavy static screen (wake/base layout, icons, background image) stays capture-replay — that is
the pixel-codec project, out of scope here. This module covers the DYNAMIC ops.
"""
import struct
import vu_crc

HERC = b"HERCULES".ljust(952, b"\0")
PKT = 952
SEQ_OFF = 958            # u16 frame counter (SM[6:8]) — stamped by the transport, not here


def pack_sm(ops, seq=0):
    """URB (HERC + SM region) carrying `ops`, CRC already valid. seq is a placeholder —
    display.Scheduler overwrites it with its session counter and re-CRCs on send.
    Long SM messages span multiple 952-byte isoc packets inside ONE URB, zero-padded to a
    packet boundary — exactly like captured element/blit URBs (1904/2856/3808/4760 bytes)."""
    ln = 8 + len(ops)
    if ln > 0x2C9F:
        raise ValueError("ops too long for the firmware validator: len %d > 0x2C9F" % ln)
    npkt = (ln + PKT - 1) // PKT
    sm = b"SM" + struct.pack("<HHH", ln, 0, seq & 0xFFFF) + bytes(ops)
    return vu_crc.fix_frame(HERC + sm.ljust(npkt * PKT, b"\0"))


def heartbeat(seq=0):
    """The ~50 Hz keepalive frame (op 0x00)."""
    return pack_sm(b"\x00", seq)


def brightness(value, seq=0):
    """Panel backlight 0..100 (op 0x31, same payload the vendor app sends on slider moves)."""
    return pack_sm(bytes([0x31, 0x01, 0x00, max(0, min(100, int(value))), 0x00]), seq)


def vu(channels, seq=0):
    """VU bar levels (op 0x40). channels = iterable of (blk, cur_l, cur_r, pk_l, pk_r); each channel
    emits its main block immediately followed by the byte-identical blk|0x80 mirror (capture order).
    pk is clamped up to cur per side (firmware invariant: the bar body never exceeds its peak cap)."""
    ops = bytearray()
    for blk, cur_l, cur_r, pk_l, pk_r in channels:
        cur_l &= 0xFF; cur_r &= 0xFF
        pk_l = max(cur_l, pk_l & 0xFF); pk_r = max(cur_r, pk_r & 0xFF)
        for b in (blk & 0x7F, (blk & 0x7F) | 0x80):
            ops += bytes([0x40, b, cur_l, cur_r, pk_l, pk_r])
    if not ops:
        raise ValueError("vu() needs at least one channel")
    ops += b"\x00"                                 # terminator after the last mirror block
    return pack_sm(bytes(ops), seq)


def generic(op, payload=b"", seq=0):
    """Any other single-op frame: ops = [op] + payload."""
    return pack_sm(bytes([op]) + bytes(payload), seq)


if __name__ == "__main__":
    # Acceptance tests: every builder's output must (a) pass the firmware-mirror validator and
    # (b) byte-match a REAL captured frame of that op, modulo seq+CRC (bytes 956..960).
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import display as D

    def norm(u):
        b = bytearray(u); b[956:960] = b"\0\0\0\0"; return bytes(b)

    def ln(u):
        return struct.unpack_from("<H", u, 954)[0]

    def check(name, built, real):
        v = vu_crc.validate(built)
        m = norm(built) == norm(real)
        print("  %-34s validate=%s  byte-match(real, mod seq+CRC)=%s"
              % (name, "ACCEPT" if v == 0 else "FAIL(%d)" % v, "OK" if m else "MISMATCH"))
        if v != 0 or not m:
            for i, (x, y) in enumerate(zip(norm(built), norm(real))):
                if x != y:
                    print("    first diff @%d: built=%02x real=%02x" % (i, x, y))
                    break
            sys.exit(1)

    mic = [u for _, u in D.urbs_with_ts("mic-input.pcapng")]
    mus = [u for _, u in D.urbs_with_ts("music-running.pcapng")]
    bri = [u for _, u in D.urbs_with_ts("brightness-changes.pcapng")]

    hb = next(u for u in mic if ln(u) == 9)
    check("heartbeat()", heartbeat(), hb)

    b31 = next(u for u in bri if D.urb_op(u) == 0x31 and ln(u) == 13)
    check("brightness(%d)" % b31[963], brightness(b31[963]), b31)

    o1 = next(u for u in mic if D.urb_op(u) == 0x40 and ln(u) == 21)
    c = o1[961], o1[962], o1[963], o1[964], o1[965]
    check("vu 1ch blk%02x" % c[0], vu([c]), o1)

    o2 = next(u for u in mus if D.urb_op(u) == 0x40 and ln(u) == 33)
    chans = [(o2[961 + k * 12], o2[962 + k * 12], o2[963 + k * 12], o2[964 + k * 12], o2[965 + k * 12])
             for k in range(2)]
    check("vu 2ch blk%02x+blk%02x" % (chans[0][0], chans[1][0]), vu(chans), o2)

    # builders are wire-valid standalone too (no carrier, no transport needed)
    for nm, f in (("heartbeat", heartbeat()), ("brightness(50)", brightness(50)),
                  ("vu solo mic 200/230", vu([(0x01, 200, 200, 230, 230)])),
                  ("vu 3ch", vu([(0x00, 10, 20, 30, 40), (0x01, 1, 2, 3, 4), (0x02, 0, 0, 0, 0)]))):
        assert vu_crc.validate(f) == 0, nm
    print("  standalone builders (incl. 3-channel vu): all ACCEPT")
    print("sm.py: ALL ACCEPTANCE TESTS PASS")
