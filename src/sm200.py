#!/usr/bin/env python3
"""sm200.py — Stream 200 XLR bulk-OUT DISPLAY frame builder: construct the device's LCD wire
frames from INTENT (the Stream-100 `sm.py` analogue, for the future 200 daemon's panel path).

STATUS: intended work-in-progress — KEEP (paired with src/stream200.py). Deliberately retained
for hardware bring-up; not dead/speculative code.

⚠ SCOPE — framing only, payload opaque. This builds the *framing / transport* layer — the
op-0x07 graphic-element chunked upload and the op-0x08 image / control frames — whose grammar is
WIRE-CONFIRMED against the capture (dev/docs/STREAM200-XLR-USB-FINDINGS.md, "Bulk-OUT display
serializer (A1′)" + the Phase-0 byte-check, 2026-06-15). The PAYLOAD bytes (element bitmaps, the
op-0x08 background pixel codec, per-field screen/LED/VU semantics) are NOT decoded and are taken
here as OPAQUE input — exactly the hardware-gated part (A2 / Track B). So sm200 reframes given
payload bytes byte-identically to the device; it does NOT yet synthesise pixels. (Project rule
"Don't guess protocol bytes": every byte below is either intent-built from the confirmed grammar
or a capture constant — `__main__` proves it reproduces all 508 captured display frames exactly.)

Wire format (NO CRC anywhere — unlike the 100's SM CRC-16; bulk transport already guarantees
delivery + retransmit, and every display frame is an explicit request→IN-ack pair):

    frame = [len:u32 LE][0x01][opcode:u8][seq:u8][0x00] [off8:u32=0]
            [off12:u32 type][off16:u32 lane] [off20:u32 run][off24:u32 rem][off28:u32 chunk]
            (op8 only: [off32:u32][off36:u32])  + chunk payload bytes
    len      = header + chunk      header = 0x20 (op7) / 0x28 (op8)
    off0 == off28 + header         (nothing trails the chunk — the hard "no CRC" evidence)
    chunking : chunk = min(remaining, BUFCAP - header), BUFCAP = 0x800
    off20 = bytes already sent (running), off24 = bytes remaining AFTER this chunk
            → off20 + off28 + off24 = element total (invariant on every frame)

  op-0x07 graphic element: total ∈ {944, 2048}; off12 = (mix<8 ? 6:5) for the 944-class /
      (mix<8 ? 10:9) for the 2048-class; off16 = lane = mix % 8. seq is PER-ELEMENT (all of an
      element's chunks share it). Dual-mix: each element is shipped once per mix — host mixes
      0..7, audience = host+8; off12 carries the host/audience select.
  op-0x08 image: a full 480×272 background = 0x20200 B, off12 = 9, header 0x28, chunked
      2008·n + 1064. seq is PER-CHUNK here (each frame advances the rolling counter). A header-only
      "end of image" terminator follows: off12 = 0x23, off20 = 0x20200, off32 = 8. Other header-only
      op-0x08 frames (off12 = 3 / 0x210 …) are device-state/screen frames whose per-field SEMANTICS
      are observe-and-copy (Track B) — `op8_control()` builds them faithfully without claiming meaning.

  seq: a single rolling u8 counter, 1..255, skipping ONLY 0x00 (0xFF is valid; the vendor app's
      captured stream cycles 254→255→1). Shared across all OUT frames; op-0x07 reuses one value for
      a whole element, op-0x08 advances per frame. Every builder takes the current counter value and
      returns (frames, next) so the caller can keep threading it. See stream200.next_seq.

Run `python3 src/sm200.py` for the acceptance tests (structural always; byte-exact vs
dev/pcap/xlr_replay_out.bin when the capture is present).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- opcodes / frame geometry (wire-confirmed) ----------------------------------------------
OP_GRAPHIC = 0x07                # graphic-element chunked upload
OP_IMAGE   = 0x08                # background image + screen/LED/VU control frames
HDR_GRAPHIC = 0x20               # op-0x07 header size (8 u32 words)
HDR_IMAGE   = 0x28               # op-0x08 header size (10 u32 words)
BUFCAP = 0x800                   # builder buffer cap: reproduces 944→[944], 2048→[2016,32],
                                 # image→[2008·n,1064] exactly (chunk = min(rem, BUFCAP-header))
ELEM_SIZES = (944, 2048)         # the only two op-0x07 element classes on the wire
IMAGE_BYTES = 0x20200            # op-0x08 background payload (full panel; pixel codec undecoded)
MIX_AUDIENCE_BASE = 8            # dual-mix: host mix 0..7, audience = host+8; off16 = mix % 8
IMG_END_OFF12 = 0x23             # op-0x08 "end of image" terminator type (off20 = image size)


def next_seq(seq):
    """Rolling u8 1..255 skipping ONLY 0x00 (0xFF valid; the vendor app's captured poll stream
    cycles 254→255→1). The one definition of the Stream 200 rolling seq — stream200.py imports it."""
    seq = (seq + 1) & 0xFF
    return seq if seq != 0x00 else 0x01


def _graphic_type(total, mix):
    """op-0x07 off12 = dual-mix type/target select. 944-class: host 6 / audience 5; 2048-class:
    host 10 / audience 9 (host = mix < 8). Wire-confirmed."""
    host = mix < MIX_AUDIENCE_BASE
    if total == 944:
        return 6 if host else 5
    if total == 2048:
        return 10 if host else 9
    raise ValueError("graphic element must be 944 or 2048 bytes, got %d" % total)


def _frame(op, seq, off12, off16, run, rem, chunk, hdr_size, off32=0, off36=0):
    """One wire frame: hdr_size-byte header + chunk bytes. off0 = total len, [0x01][op][seq][0x00]
    marker, off8 = 0, off12/off16/off20(run)/off24(rem)/off28(chunk len)[, off32, off36]. NO CRC."""
    h = bytearray(hdr_size)
    struct.pack_into("<I", h, 0, hdr_size + len(chunk))
    h[4] = 0x01
    h[5] = op & 0xFF
    h[6] = seq & 0xFF
    h[7] = 0x00
    struct.pack_into("<I", h, 12, off12)
    struct.pack_into("<I", h, 16, off16)
    struct.pack_into("<I", h, 20, run)
    struct.pack_into("<I", h, 24, rem)
    struct.pack_into("<I", h, 28, len(chunk))
    if hdr_size >= 0x28:
        struct.pack_into("<I", h, 32, off32)
        struct.pack_into("<I", h, 36, off36)
    return bytes(h) + bytes(chunk)


# --------------------------------------------------------------------------- op-0x07 graphic
def graphic_element(payload, mix, seq):
    """Ship one graphic element (a 944- or 2048-byte element bitmap) for one dual-mix source.
    `payload` bytes are opaque here (the element-bitmap encoding is hardware-gated). All chunks
    share `seq` (op-0x07 seq is per-element). Returns (frames, next_seq): 1 frame for a 944-class
    element, 2 (2016+32) for a 2048-class one. Call once per mix for dual-mix duplication."""
    total = len(payload)
    off12 = _graphic_type(total, mix)         # also validates the size class
    off16 = mix % MIX_AUDIENCE_BASE
    cap = BUFCAP - HDR_GRAPHIC
    out, run = [], 0
    while run < total:
        chunk = payload[run:run + min(total - run, cap)]
        out.append(_frame(OP_GRAPHIC, seq, off12, off16, run, total - run - len(chunk),
                          chunk, HDR_GRAPHIC))
        run += len(chunk)
    return out, next_seq(seq)                 # one element consumes exactly one seq


# --------------------------------------------------------------------------- op-0x08 image
def background(image, seq):
    """Upload a full panel background (`image` must be IMAGE_BYTES; the pixel codec is NOT decoded,
    so the bytes are opaque / observe-and-copy). off12 = 9, chunked at BUFCAP. Unlike op-0x07 each
    chunk advances the rolling counter (op-0x08 seq is per-frame). Returns (frames, next_seq);
    follow with background_end(next_seq)."""
    if len(image) != IMAGE_BYTES:
        raise ValueError("background image must be %d (0x%x) bytes, got %d"
                         % (IMAGE_BYTES, IMAGE_BYTES, len(image)))
    cap = BUFCAP - HDR_IMAGE
    out, run, s = [], 0, seq
    while run < len(image):
        chunk = image[run:run + min(len(image) - run, cap)]
        out.append(_frame(OP_IMAGE, s, 9, 0, run, len(image) - run - len(chunk), chunk, HDR_IMAGE))
        run += len(chunk)
        s = next_seq(s)
    return out, s


def background_end(seq):
    """The op-0x08 "end of image" terminator (header-only): off12 = 0x23, off20 = image size,
    off32 = 8. Wire-confirmed; sent after background(). Returns ([frame], next_seq)."""
    return [_frame(OP_IMAGE, seq, IMG_END_OFF12, 0, IMAGE_BYTES, 0, b"", HDR_IMAGE, off32=8)], \
        next_seq(seq)


def op8_control(seq, code, run=0, rem=0, off32=0, off36=0):
    """A header-only op-0x08 device-state/screen frame (off28 = 0). `code` = off12 (e.g. 3, 0x210
    seen in the capture). The per-field SEMANTICS are NOT decoded (Track B / observe-and-copy):
    this builds the frame faithfully from given field values but assigns them no meaning. Returns
    ([frame], next_seq)."""
    return [_frame(OP_IMAGE, seq, code, 0, run, rem, b"", HDR_IMAGE, off32=off32, off36=off36)], \
        next_seq(seq)


# --------------------------------------------------------------------------- acceptance tests
def _read_replay():
    """Captured bulk-OUT frames from dev/pcap/xlr_replay_out.bin ([u32 len][bytes]…), or None if
    the dev capture isn't present (public tree)."""
    try:
        from paths import PCAP
    except Exception:
        return None
    path = os.path.join(PCAP, "xlr_replay_out.bin")
    if not os.path.exists(path):
        return None
    blob = open(path, "rb").read()
    frames, i = [], 0
    while i + 4 <= len(blob):
        (n,) = struct.unpack_from("<I", blob, i)
        i += 4
        frames.append(blob[i:i + n])
        i += n
    return frames


def _u32(f, o):
    return struct.unpack_from("<I", f, o)[0]


def _selftest_structural():
    ok = True

    def check(name, cond):
        nonlocal ok
        print("  %-52s %s" % (name, "OK" if cond else "FAIL"))
        ok = ok and cond

    # 944-class element, host mix 0 → one 976-byte frame, off12=6, lane 0, seq reused, no trailer.
    fr, nxt = graphic_element(b"\x11" * 944, mix=0, seq=200)
    f = fr[0]
    check("944 host: 1 frame, len 976, off12=6, lane=0",
          len(fr) == 1 and len(f) == 976 and _u32(f, 12) == 6 and _u32(f, 16) == 0)
    check("944: header marker [01 07 seq 00], off8=0, off0==off28+0x20",
          f[4] == 1 and f[5] == 7 and f[6] == 200 and f[7] == 0 and _u32(f, 8) == 0
          and _u32(f, 0) == _u32(f, 28) + 0x20 and _u32(f, 28) == 944)
    check("944: run+chunk+rem == 944, payload round-trips, one seq consumed",
          _u32(f, 20) + _u32(f, 28) + _u32(f, 24) == 944 and f[0x20:] == b"\x11" * 944
          and nxt == 201)

    # 2048-class element, audience mix 8+2 → [2016,32] split, off12=9, lane 2, shared seq.
    fr2, _ = graphic_element(b"\x22" * 2048, mix=MIX_AUDIENCE_BASE + 2, seq=201)
    check("2048 audience: 2 frames [2048,64], off12=9, lane=2, shared seq",
          [len(x) for x in fr2] == [2048, 64] and all(_u32(x, 12) == 9 for x in fr2)
          and all(_u32(x, 16) == 2 for x in fr2) and {x[6] for x in fr2} == {201})
    check("2048: chunks 2016+32, run/rem accounting, payload reassembles",
          [_u32(x, 28) for x in fr2] == [2016, 32]
          and [(_u32(x, 20), _u32(x, 24)) for x in fr2] == [(0, 32), (2016, 0)]
          and fr2[0][0x20:] + fr2[1][0x20:] == b"\x22" * 2048)

    # host/audience type table for both size classes.
    check("type table: 944 host=6/aud=5, 2048 host=10/aud=9",
          _graphic_type(944, 0) == 6 and _graphic_type(944, 8) == 5
          and _graphic_type(2048, 0) == 10 and _graphic_type(2048, 8) == 9)

    # background image: chunked 2008·n + 1064, off12=9, per-frame seq, full byte accounting.
    bg, nxt = background(b"\x00" * IMAGE_BYTES, seq=58)
    sizes = [_u32(x, 28) for x in bg]
    check("background: off12=9, 0x28 header, sum(off28)==0x20200, last chunk 1064",
          all(_u32(x, 12) == 9 for x in bg) and all(len(x) == 0x28 + _u32(x, 28) for x in bg)
          and sum(sizes) == IMAGE_BYTES and sizes[-1] == 1064 and sizes[:-1] == [2008] * (len(bg) - 1))
    check("background: per-frame seq advances (58,59,…), 66 frames",
          [x[6] for x in bg[:3]] == [58, 59, 60] and len(bg) == 66 and nxt == 58 + 66)

    # terminator + a header-only control frame.
    end, _ = background_end(124)
    e = end[0]
    check("background_end: len 40, off12=0x23, off20=image size, off32=8",
          len(e) == 40 and _u32(e, 12) == 0x23 and _u32(e, 20) == IMAGE_BYTES and _u32(e, 32) == 8)
    ctl, _ = op8_control(57, 0x210, off36=0x300)
    check("op8_control(0x210, off36=0x300): len 40, header-only",
          len(ctl[0]) == 40 and _u32(ctl[0], 12) == 0x210 and _u32(ctl[0], 36) == 0x300
          and _u32(ctl[0], 28) == 0)

    # input validation.
    bad = False
    try:
        graphic_element(b"\x00" * 100, 0, 1)
    except ValueError:
        bad = True
    try:
        background(b"\x00" * 10, 1)
    except ValueError:
        bad = bad and True
    check("rejects wrong-size element / image payloads", bad)
    return ok


def rebuild_check(frames):
    """Rebuild every captured op-0x07 / op-0x08 display frame FROM INTENT and compare byte-exact.
    `frames` = a list of bulk-OUT frame byte-strings (e.g. parsed from dev/pcap/xlr_replay_out.bin).
    Pure computation — no I/O, no printing — so any caller can report it as it likes (the selftest
    below; the dev bring-up probe's AppImage selftest, which passes its bundled replay blob here).
    Returns a dict: op7_total/op7_ok/op7_elems, op8_total/op8_ok, disp_total/disp_ok, ok, and a
    list of `notes` (empty on success; the first mismatch on failure)."""
    disp = [f for f in frames if len(f) >= 8 and f[5] in (OP_GRAPHIC, OP_IMAGE)]
    notes = []

    # --- op-0x07: group chunks into elements (rem==0 closes one), recover (payload, mix, seq) ---
    op7 = [f for f in disp if f[5] == OP_GRAPHIC]
    elems, cur = [], []
    for f in op7:
        cur.append(f)
        if _u32(f, 24) == 0:
            elems.append(cur)
            cur = []
    n7 = 0
    for el in elems:
        payload = b"".join(f[HDR_GRAPHIC:] for f in el)
        off12, lane, seq = _u32(el[0], 12), _u32(el[0], 16), el[0][6]
        mix = lane + (0 if off12 in (6, 10) else MIX_AUDIENCE_BASE)   # host:6/10  audience:5/9
        try:
            built, _ = graphic_element(payload, mix, seq)
        except ValueError as e:
            notes.append("op7 build error (seq=%d off12=%#x): %s" % (seq, off12, e))
            break
        if built == el:
            n7 += len(el)
        else:
            notes.append("op7 MISMATCH (seq=%d off12=%#x)" % (seq, off12))
            break

    # --- op-0x08: background images (off12=9, run==0 starts one) + header-only control frames ---
    op8 = [f for f in disp if f[5] == OP_IMAGE]
    n8, i = 0, 0
    while i < len(op8) and not notes:
        f = op8[i]
        if _u32(f, 12) == 9:                       # background image: gather until rem==0
            grp = []
            while i < len(op8) and _u32(op8[i], 12) == 9:
                grp.append(op8[i])
                i += 1
                if _u32(grp[-1], 24) == 0:
                    break
            image = b"".join(g[HDR_IMAGE:] for g in grp)
            try:
                built, _ = background(image, grp[0][6])
            except ValueError as e:
                notes.append("op8 background error (start seq=%d): %s" % (grp[0][6], e))
                break
            if built == grp:
                n8 += len(grp)
            else:
                notes.append("op8 background MISMATCH (start seq=%d)" % grp[0][6])
                break
        else:                                      # header-only control / terminator frame
            code, seq = _u32(f, 12), f[6]
            if code == IMG_END_OFF12:
                built, _ = background_end(seq)
            else:
                built, _ = op8_control(seq, code, run=_u32(f, 20), rem=_u32(f, 24),
                                       off32=_u32(f, 32), off36=_u32(f, 36))
            if built[0] == f:
                n8 += 1
            else:
                notes.append("op8 control MISMATCH (off12=%#x seq=%d)" % (code, seq))
                break
            i += 1

    ok = n7 == len(op7) and n8 == len(op8) and not notes
    return {"op7_total": len(op7), "op7_ok": n7, "op7_elems": len(elems),
            "op8_total": len(op8), "op8_ok": n8,
            "disp_total": len(disp), "disp_ok": n7 + n8, "ok": ok, "notes": notes}


def _selftest_capture(frames):
    """Byte-exact proof against the real wire (the WIRE-CONFIRMED grammar) — prints rebuild_check."""
    r = rebuild_check(frames)
    for note in r["notes"]:
        print("    " + note)
    print("  op-0x07: %d/%d frames in %d elements rebuilt byte-exact: %s"
          % (r["op7_ok"], r["op7_total"], r["op7_elems"],
             "OK" if r["op7_ok"] == r["op7_total"] else "FAIL"))
    print("  op-0x08: %d/%d frames (backgrounds + control) rebuilt byte-exact: %s"
          % (r["op8_ok"], r["op8_total"], "OK" if r["op8_ok"] == r["op8_total"] else "FAIL"))
    print("  total display frames reproduced byte-exact: %d/%d" % (r["disp_ok"], r["disp_total"]))
    return r["ok"]


def selftest():
    print("sm200 structural selftest:")
    ok = _selftest_structural()
    frames = _read_replay()
    if frames is None:
        print("\nsm200 capture byte-check: SKIP (dev/pcap/xlr_replay_out.bin not present)")
    else:
        print("\nsm200 capture byte-check (vs dev/pcap/xlr_replay_out.bin):")
        ok = _selftest_capture(frames) and ok
    print("\nsm200 selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest())
