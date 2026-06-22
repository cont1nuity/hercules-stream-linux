#!/usr/bin/env python3
"""Dependency-free USBPcap (LINKTYPE_USBPCAP, pcapng) parser for the Stream 100 capture.

No tshark/scapy needed. Yields per-URB records with transfer type, endpoint, direction,
control-setup bytes and the data payload, so we can diff the official app's USB traffic and
recover the LED / brightness / screen wire format.

USBPcap packet header (little-endian):
  off 0  u16 headerLen      (data starts here)
  off 2  u64 irpId
  off 10 u32 status
  off 14 u16 function
  off 16 u8  info           (bit0 = PDO->FDO, i.e. 1 = from device / IN)
  off 17 u16 bus
  off 19 u16 device
  off 21 u8  endpoint       (incl 0x80 dir bit)
  off 22 u8  transfer       (0=isoc 1=intr 2=ctrl 3=bulk)
  off 23 u32 dataLength
  control adds u8 stage at off 27 (0=SETUP 1=DATA 2=STATUS); setup stage payload = 8B setup pkt
"""
import struct, sys, collections

TT = {0: "isoc", 1: "intr", 2: "ctrl", 3: "bulk"}


def _epbs(d):
    off = 0
    while off + 8 <= len(d):
        btype, blen = struct.unpack_from("<II", d, off)
        if blen < 12 or off + blen > len(d):
            break
        if btype == 6:  # Enhanced Packet Block
            tshi, tslo = struct.unpack_from("<II", d, off + 12)
            ts = (tshi << 32) | tslo
            caplen = struct.unpack_from("<I", d, off + 8 + 12)[0]
            data = d[off + 8 + 20:off + 8 + 20 + caplen]
            yield ts, data
        off += blen


class Rec:
    __slots__ = ("ts", "transfer", "ep", "dir_in", "stage", "setup", "data")

    def __repr__(self):
        s = self.setup.hex() if self.setup else "-"
        return f"{TT.get(self.transfer,self.transfer)} ep{self.ep:02x} {'IN' if self.dir_in else 'OUT'} setup={s} dlen={len(self.data)}"


def parse(path):
    d = open(path, "rb").read()
    out = []
    for ts, pkt in _epbs(d):
        if len(pkt) < 27:
            continue
        hlen = struct.unpack_from("<H", pkt, 0)[0]
        info = pkt[16]
        ep = pkt[21]
        transfer = pkt[22]
        dlen = struct.unpack_from("<I", pkt, 23)[0]
        payload = pkt[hlen:hlen + dlen]
        r = Rec()
        r.ts = ts
        r.transfer = transfer
        r.ep = ep
        r.dir_in = bool(ep & 0x80)
        r.stage = pkt[27] if (transfer == 2 and hlen >= 28) else None
        r.setup = None
        r.data = payload
        # control SETUP stage: 8-byte setup packet is the payload
        if transfer == 2 and r.stage == 0 and len(payload) >= 8:
            r.setup = payload[:8]
            r.data = payload[8:]
        out.append(r)
    return out


def out_data_frames(recs, transfers=(0, 2, 3)):
    """Host->device data-bearing frames (skip zero-length, skip control status)."""
    for r in recs:
        if r.dir_in:
            continue
        if r.transfer not in transfers:
            continue
        if r.transfer == 2:
            # vendor/class control OUT (skip standard requests like SET_CONFIG/SET_IFACE)
            if r.setup is None:
                continue
            bmrt = r.setup[0]
            if (bmrt & 0x60) == 0:  # standard request type
                continue
            yield r
        else:
            if len(r.data) == 0:
                continue
            yield r


if __name__ == "__main__":
    import os
    from paths import PCAP
    paths = sys.argv[1:] or sorted(
        os.path.join(PCAP, f) for f in os.listdir(PCAP) if f.endswith(".pcapng"))
    for p in paths:
        recs = parse(p)
        c = collections.Counter()
        for r in recs:
            c[(TT.get(r.transfer, r.transfer), "%02x" % r.ep, "IN" if r.dir_in else "OUT")] += 1
        print(f"\n== {os.path.basename(p)}  ({len(recs)} urbs)")
        for k, v in c.most_common():
            print(f"  {v:5d}  {k}")
