#!/usr/bin/env python3
"""Element-layer slot test — confirm the firmware's fixed 2x4 element grid on hardware.

From the firmware op parser (FUN_08009930, artifacts/fw-op40.c:2166, grammar in op_walk.py):
  op35 icon  = [35][row][slot][datalen:u16][RLE 32 rows]  -> buf(row) + (slot&3)*0xc00
  op36 label = [36][row][slot][datalen:u16][RLE 16 rows]  -> buf(row) + slot*0x14a0
  op41 vol   = [41][id][val:u16][val2:u16]  id&3=ch, bit7=button bank; percent = val*100/0xffff
  op30 state = [30][ch][state]              state 0=off 1=on 2=on+alt
There is NO X/Y on the wire: row (b5) picks the dial/button row, slot (b6) the column.
Screen coordinates per slot live in the firmware renderer.

What this sends (after the normal wake + base replay) and what the photo must show:
  phase 1  icon  row0 slot0  solid MAGENTA + white corner   -> dial row, column 1
           icon  row0 slot2  solid GREEN   + white corner   -> dial row, column 3
           icon  row1 slot1  solid CYAN    + white corner   -> button row, column 2
  phase 2  label row0 slot1  RED|WHITE|BLUE vertical bands  -> dial row, column 2
           label row1 slot3  YELLOW|BLACK bands             -> button row, column 4
  phase 3  op41 ch1 (both banks) val=0x4000                 -> dial 2 volume reads "25"
  phase 4  op30 ch3 state=2                                 -> channel 4 alt/highlight state
If an element lands in a different cell than listed, the slot mapping is wrong — note which
cell it actually hit.

Usage:
  python3 element_test.py --selftest    # offline: walk + CRC-validate every authored frame
  python3 element_test.py [--secs 60]   # hardware run (device must be plugged in)
"""
import sys, os, struct, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sm
import vu_crc
import op_walk
import codec_element as ce

W_ICON, H_ICON = 32, 32
W_LBL, H_LBL = 110, 16
OPQ = 0xF << 16          # nib 0xf = opaque (captured icons use nib as the high/alpha nibble)


def icon_px(color565):
    """Solid color icon with an 8x8 white top-left corner notch (orientation marker)."""
    px = [OPQ | color565] * (W_ICON * H_ICON)
    for y in range(8):
        for x in range(8):
            px[y * W_ICON + x] = OPQ | 0xFFFF
    return px


def label_px(colors565):
    """Vertical bands across the 110x16 label."""
    n = len(colors565)
    return [OPQ | colors565[min((x * n) // W_LBL, n - 1)]
            for _y in range(H_LBL) for x in range(W_LBL)]


def elem_frame(op, row, slot, px, w, h):
    rle = bytes(ce.rle_encode(px, w, h))
    ops = bytes([op, row, slot]) + struct.pack("<H", len(rle)) + rle
    return sm.pack_sm(ops)


def build_phases():
    return [
        ("icons -> dial s0 MAGENTA, dial s2 GREEN, button s1 CYAN", [
            elem_frame(0x35, 0, 0, icon_px(0xF81F), W_ICON, H_ICON),
            elem_frame(0x35, 0, 2, icon_px(0x07E0), W_ICON, H_ICON),
            elem_frame(0x35, 1, 1, icon_px(0x07FF), W_ICON, H_ICON),
        ]),
        ("labels -> dial s1 R|W|B, button s3 Y|K", [
            elem_frame(0x36, 0, 1, label_px([0xF800, 0xFFFF, 0x001F]), W_LBL, H_LBL),
            elem_frame(0x36, 1, 3, label_px([0xFFE0, 0x0000, 0xFFE0]), W_LBL, H_LBL),
        ]),
        ("op41 ch1 -> dial 2 volume display = 25", [
            sm.generic(0x41, bytes([0x01]) + struct.pack("<HH", 0x4000, 0x4000)),
            sm.generic(0x41, bytes([0x81]) + struct.pack("<HH", 0x4000, 0x4000)),
        ]),
        ("op30 ch3 -> state 2 (alt/highlight)", [
            sm.generic(0x30, bytes([0x03, 0x02])),
        ]),
    ]


def selftest():
    ok = True
    for name, frames in build_phases():
        for f in frames:
            v = vu_crc.validate(f)
            ln = struct.unpack_from("<H", f, 954)[0]
            ops = f[960:952 + ln]
            try:
                recs = list(op_walk.walk(ops))
            except Exception as e:
                print(f"  WALK FAIL [{name}]: {e}"); ok = False; continue
            r = recs[0]
            print(f"  [{name}] op{r[1]:02x} paylen={len(r[2])} smlen={ln} "
                  f"crc={'ACCEPT' if v == 0 else 'REJECT'}")
            if v != 0 or len(recs) != 1:
                ok = False
    # round-trip: authored RLE must decode back to the authored pixels
    px = icon_px(0xF81F)
    dec, _ = ce.rle_decode(bytes(ce.rle_encode(px, W_ICON, H_ICON)), W_ICON, H_ICON)
    print(f"  icon RLE round-trip: {'OK' if dec == px else 'MISMATCH'}")
    print("element_test selftest:", "PASS" if ok and dec == px else "FAIL")
    return 0 if ok and dec == px else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    import display as D
    secs = float(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 60.0

    wake = [u for _, u in D.urbs_with_ts("baseline-app-starting.pcapng")]
    dev, ep = D.open_ep()
    s = D.Scheduler(ep)
    print("priming + wake/base replay (%d frames)..." % len(wake))
    for i in range(25):
        s.send(sm.heartbeat())
    for u in wake:
        s.send(u)
    print("base painted; holding 3s before authored elements")
    t_phase = s.elapsed() + 3.0

    phases = build_phases()
    for name, frames in phases:
        while s.elapsed() < t_phase:
            s.send(sm.heartbeat())
        print("PHASE: %s" % name)
        for _ in range(3):                      # repaint passes: isoc is fire-and-forget
            for f in frames:
                s.send(f)
            for _k in range(3):
                s.send(sm.heartbeat())
        t_phase = s.elapsed() + 3.0

    print("all phases sent; holding for photo (Ctrl-C or --secs %.0f)" % secs)
    try:
        while s.elapsed() < secs:
            s.send(sm.heartbeat())
    except KeyboardInterrupt:
        pass
    finally:
        import usb.util
        usb.util.release_interface(dev, D.IFACE)
        usb.util.dispose_resources(dev)
        print("done (panel blanks without heartbeats).")


if __name__ == "__main__":
    main()
