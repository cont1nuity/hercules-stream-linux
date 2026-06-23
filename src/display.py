#!/usr/bin/env python3
"""Stream 100 display daemon: wake the panel and show the base layout by replaying the
official app's captured draw traffic, then hold it lit with heartbeats.

Reverse-engineered model (see CLAUDE.md "Display wire format"):
  - Display = isoc EP 0x01 (iface1 alt1), 952-byte packets. Logical frame = "HERCULES"
    sync packet + "SM" message packet. SM = "SM" + u16 len + u16 CRC16 + u16 field + ops.
    The CRC16 (SM[4:6], URB offset 956) is checked by the device and a bad CRC drops the
    whole frame (see vu_crc.py). op 0x00 = heartbeat (len 9); op 0x30-0x38 = pixel-blit.
    NOTE: offset 956 is the CRC, NOT a timestamp — earlier code misread it as "ts:u32".
  - The panel has no framebuffer across power cycles, BLANKS when the heartbeat stops, and
    its render pipeline only stays awake while a steady heartbeat cadence flows. Draws sent
    without that cadence are ignored (panel stays dark).
  - Isoc OUT is fire-and-forget; the app streams ~1 kHz so drops self-heal.

Approach (this is what reliably lights the panel):
  - send raw captured draw URBs VERBATIM (preserve framing + original timestamps),
  - keep a heartbeat flowing before, between, and after every draw,
  - collapse the background color-cycle to one fill (deterministic color),
  - pace each write by isoc-packet count and paint twice for drop-redundancy.

Usage:
  python3 display.py                 # wake + base layout, hold until Ctrl-C
  python3 display.py --secs 30       # hold 30s then exit (panel blanks on exit)
  python3 display.py --pace 2        # ms per isoc packet (default 2)
  python3 display.py --passes 3      # paint passes for redundancy (default 2)
"""
import sys, os, time, struct
import usb.core, usb.util
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcapdec as P
import usbdev
import vu_crc
from paths import PCAP

VID, PID = 0x06F8, 0xE053
IFACE, ALT, EP = 1, 1, 0x01
PKT = 952
HERC = b"HERCULES".ljust(PKT, b"\x00")


def urb_op(u):
    """opcode of a raw isoc OUT URB ([HERC][SM]); None if not an SM draw."""
    if len(u) >= 961 and u[952:954] == b"SM":
        ln = struct.unpack_from("<H", u, 954)[0]
        if ln > 4:
            return u[960]      # payload[4] = 956 + 4
    return None


def open_ep():
    dev = usbdev.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("device 06f8:e053 not found (plugged in? udev rule installed?)")
    try:
        if dev.is_kernel_driver_active(IFACE):
            dev.detach_kernel_driver(IFACE)
    except Exception:
        pass
    claimed = False
    for attempt in range(6):
        try:
            usb.util.claim_interface(dev, IFACE)
            claimed = True
            break
        except usb.core.USBError:
            if attempt == 2:
                try: dev.reset()                 # clear a lingering claim from a killed run
                except Exception: pass
            time.sleep(0.5)
    if not claimed:
        sys.exit("iface1 busy after retries — a previous run is still holding it; kill it.")
    dev.set_interface_altsetting(interface=IFACE, alternate_setting=ALT)
    intf = dev.get_active_configuration()[(IFACE, ALT)]
    ep = usb.util.find_descriptor(intf, custom_match=lambda e: e.bEndpointAddress == EP)
    if ep is None:
        sys.exit("isoc OUT endpoint 0x01 not found")
    return dev, ep


def urbs_with_ts(name):
    """isoc OUT URBs (verbatim) with capture timestamps."""
    path = name if os.path.sep in name else os.path.join(PCAP, name)
    return [(r.ts, bytes(r.data)) for r in P.parse(path)
            if not r.dir_in and r.transfer == 0 and r.ep == EP and len(r.data)]


# The app drives the isoc pipe at ~50 Hz: one HERC+SM URB every ~20 ms (NOT 1 ms). Idle = a
# heartbeat every slot; control frames (brightness/VU) replace a heartbeat in their slot. The
# render pipeline stays awake only while a steady heartbeat cadence flows. Frames are sent
# VERBATIM except two transport fields the Scheduler owns: the seq counter (offset 958, one
# monotonic +1-per-frame session counter like real HW) and the SM CRC-16 (offset 956),
# recomputed per frame so authored/spliced payloads pass the firmware validator (see vu_crc.py).
SLOT = 0.020
SEQ_OFF = 958            # u16 SM frame counter (SM[6:8]); validator stores it, parser ignores it


class Scheduler:
    """Precise absolute-deadline sender: each frame goes out on its 20 ms slot, no drift.
    Owns the SM seq counter (+1 per frame, like real HW — kills the carrier-loop seq repeat)
    and recomputes the SM CRC-16 on every outgoing frame (the device drops a stale-CRC frame)."""
    def __init__(self, ep, seq0=1, dev=None):
        self.ep = ep
        self.t0 = time.monotonic()
        self.slot = 0
        self.seq = seq0 & 0xFFFF       # real HW starts a session at seq=1 (baseline capture)
        # Surprise-removal guard. An isochronous OUT on a yanked device makes pyusb pass a
        # NEGATIVE iso-packet count to libusb_alloc_transfer (it never checks
        # libusb_get_max_iso_packet_size's return), which abort()s the WHOLE PROCESS — a C
        # assertion, not a catchable Python exception, so the try/except below can't save it.
        # Gate every write on the device's usbfs node still existing (bus/address are fixed for
        # this session). dev=None -> no gate (manual display.py tools, never the daemon).
        self._devnode = None
        if dev is not None:
            try:
                self._devnode = "/dev/bus/usb/%03d/%03d" % (dev.bus, dev.address)
            except Exception:
                self._devnode = None
        self.device_gone = False

    def send(self, frame):
        if self.device_gone:                        # device removed -> nothing to send (idle)
            return
        # The device drops any SM frame whose CRC-16 (SM[4:6], offset 956) does not match its
        # payload (firmware FUN_08008efc) — the op parser is never reached. So stamp the session
        # seq, THEN recompute the CRC here, the one place every outgoing frame passes, AFTER any
        # caller edits. (Offset 956 is the CRC, NOT a ts — the old "stamp a monotonic ts at 956"
        # overwrote the CRC and got every authored frame silently rejected.)
        if len(frame) >= vu_crc.CRC_OFF + 2 and frame[952:954] == b"SM":
            frame = bytearray(frame)
            struct.pack_into("<H", frame, SEQ_OFF, self.seq)
            self.seq = (self.seq + 1) & 0xFFFF
            vu_crc.fix_frame_inplace(frame)
        deadline = self.t0 + self.slot * SLOT
        now = time.monotonic()
        if deadline > now:
            time.sleep(deadline - now)              # absolute schedule -> no cumulative drift
        elif now - deadline > 0.5:                  # fell far behind -> resync, don't burst
            self.t0 = now - self.slot * SLOT
        if self._devnode is not None and not os.path.exists(self._devnode):
            self.device_gone = True                 # an isoc write on a removed device aborts()
            return                                  # the whole process (libusb assert) — skip it
        try:
            self.ep.write(frame, timeout=200)
        except usb.core.USBError:
            pass
        self.slot += 1

    def elapsed(self):
        return time.monotonic() - self.t0


def main():
    a = sys.argv[1:]
    def opt(name, default):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    secs = opt("--secs", None);    secs = float(secs) if secs else None
    brightness = opt("--brightness", None); brightness = int(brightness) if brightness else None
    wake_cap = opt("--wake", "baseline-app-starting.pcapng")
    play_cap = opt("--play", None)   # after wake/base, stream this capture VERBATIM, then hold
    image = opt("--image", None)     # after wake/base, draw this PNG as the 480x272 background
    img_carrier = opt("--image-carrier", "background-color-change-alphabetical.pcapng")
    img_passes = int(opt("--image-passes", "2"))
    demo = "--demo" in a
    if demo: a.remove("--demo")

    wake = [u for _, u in urbs_with_ts(wake_cap)]
    # idle = the actual heartbeat frames the app sends, taken verbatim from the capture
    idle = [u for u in wake if urb_op(u) == 0x00]

    # brightness carrier: the real brightness-changes stream (heartbeats + op31); we overwrite
    # only the op31 value byte. The Scheduler recomputes the CRC after that edit, so the device
    # accepts each authored op31 frame (a stale CRC would otherwise drop it).
    control = brightness is not None or demo
    bstream = [u for _, u in urbs_with_ts("brightness-changes.pcapng")] if control else []
    def with_brightness(u, v):
        if urb_op(u) == 0x31 and struct.unpack_from("<H", u, 954)[0] == 13:
            b = bytearray(u); b[963] = max(0, min(100, int(v))); return bytes(b)
        return u

    print("wake/base from %s (%d frames, %d idle hb)%s%s" % (wake_cap, len(wake), len(idle),
          "" if brightness is None else ", brightness=%d" % brightness,
          " [demo: 100<->8 every 6s]" if demo else ""))

    dev, ep = open_ep()
    print("device awake: iface%d alt%d ep0x%02x; slot=%.0fms" % (IFACE, ALT, EP, SLOT * 1000))
    s = Scheduler(ep, dev=dev)

    for i in range(25):                              # idle prime (~0.5s of real heartbeats)
        s.send(idle[i % len(idle)])
    for u in wake:                                   # faithful wake/base, one frame per slot
        s.send(u)
    print("base painted. idle heartbeat @50Hz%s. (Ctrl-C to stop; panel blanks on exit)" %
          ("" if secs is None else " for %.0fs" % secs))

    if play_cap:
        # observe-and-copy: stream the whole event capture VERBATIM (heartbeats + draws,
        # each frame's real ts), one frame per 20 ms slot. No stripping, no synthesis.
        event = [u for _, u in urbs_with_ts(play_cap)]
        print("playing %s verbatim: %d frames (~%.1fs) then hold" %
              (play_cap, len(event), len(event) * SLOT))
        for u in event:
            s.send(u)
        print("event done; holding with idle heartbeats.")

    if image:
        # observe-and-copy: take REAL op38 + 31 op37 carrier URBs (consecutive -> real
        # advancing ts) and overwrite ONLY palette + index bytes with our image (codec_encode).
        import codec_encode
        carrier_path = img_carrier if os.path.sep in img_carrier else os.path.join(PCAP, img_carrier)
        frames, pal, _ = codec_encode.encode(image, carrier_path)
        print("drawing %s: %d frames (1 op38 + %d op37), %d colors, %d passes" %
              (image, len(frames), len(frames) - 1, len(set(pal)), img_passes))
        for _ in range(max(1, img_passes)):     # repaint passes for isoc drop-redundancy
            for u in frames:
                s.send(u)
            for k in range(3):                   # a few heartbeats between passes
                s.send(idle[k % len(idle)])
        print("image drawn; holding with idle heartbeats.")

    # maintain:
    #   - no brightness control -> loop the real idle heartbeats verbatim
    #   - brightness control     -> loop the real brightness-changes stream (real heartbeats +
    #     op31 with advancing ts), overwriting ONLY byte 963 (value) on each op31 frame
    i = 0; phase = -1
    try:
        while secs is None or s.elapsed() < secs:
            if not control:
                s.send(idle[i % len(idle)]); i += 1; continue
            if demo:
                p = int(s.elapsed() / 6) % 2; v = 100 if p == 0 else 8
                if p != phase: print("  demo brightness=%d" % v); phase = p
            else:
                v = brightness
            u = bstream[i % len(bstream)]
            s.send(with_brightness(u, v)); i += 1
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        usb.util.release_interface(dev, IFACE)
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
