#!/usr/bin/env python3
"""Live audio level source for the Stream 100 VU bars (op40) — the daemon's metering primitives.

`Meter` reads a PipeWire monitor/source (or one app's sink-input stream) through a non-blocking
`parec` peak tap; `to_byte` maps an audio envelope to an op40 level byte. The daemon (ui.py)
builds the op40 frames itself (sm.vu) and imports only `Meter`, `to_byte` and `_die_with_parent`.

Wire model (VERIFIED 2026-06-10 from captures + SDK decompile): op40 block =
[40, blk, L0,L1,L2,L3] (+ mirror blk|0x80). L0/L1 = live level, L2/L3 = the displayed bar (a
host peak-hold). blk 0x00 = master, 0x01 = mic, 0x02 = playback. All metering is read-only.
"""
import os, subprocess, fcntl, array, math


def _die_with_parent():
    """child pre-exec: PR_SET_PDEATHSIG -> the kernel SIGTERMs parec if our process dies for
    ANY reason (crash, SIGKILL, ...) — no orphaned recording streams in the mixer."""
    try:
        import ctypes, signal as _sig
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _sig.SIGTERM, 0, 0, 0)
    except Exception:
        pass


class Meter:
    """non-blocking peak reader via parec (s16le mono): either a source/monitor `device`
    (e.g. "Master.monitor", "@DEFAULT_MONITOR@") or one app's stream via `monitor_stream`
    (a sink-input index — parec --monitor-stream). Read-only in both modes. The parec child
    is bound to our lifetime (pdeathsig), so it can never outlive the daemon."""
    def __init__(self, device=None, rate=24000, monitor_stream=None, release=0.82):
        # release = body fall smoothing PER peak() CALL (attack is always instant). 0.82 was
        # tuned for a ~50 Hz caller; a 25 Hz caller (ui.py) wants ~0.5 or the bar feels laggy.
        self.release = release
        sel = (["--monitor-stream=%d" % monitor_stream] if monitor_stream is not None
               else ["--device=" + device])
        self.proc = subprocess.Popen(
            ["parec", *sel, "--client-name=hercules-stream-vu", "--format=s16le",
             "--rate=%d" % rate, "--channels=1", "--raw", "--latency-msec=15"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)
        self.fd = self.proc.stdout.fileno()
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self.env = 0.0
    def peak(self):
        try:
            data = os.read(self.fd, 1 << 16)
        except (BlockingIOError, OSError):
            data = b""
        if data:
            a = array.array('h'); a.frombytes(data[:len(data) // 2 * 2])
            pk = (max((abs(x) for x in a), default=0)) / 32768.0
        else:
            pk = 0.0
        if pk > self.env:                       # fast attack
            self.env = pk
        else:                                    # release (smoothing factor per call)
            self.env = self.env * self.release + pk * (1.0 - self.release)
        return self.env
    def close(self):
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.5)     # reap — a terminate alone leaves a zombie
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=0.5)
        except Exception:
            pass


LMAX = 0x73   # captured op40 levels span 0x00..0x73; bit7 (0x80) is the mirror flag
FLOOR_DB = 50.0   # dBFS mapped to bar bottom (-50 dB -> 0, 0 dB -> full 0x73)


def to_byte(env, gain):
    """audio envelope (0..1) -> op40 level byte over the FULL range 0x00..0x73 (dB-scaled).
    Captures show bars render across the whole range, not just a high band."""
    e = min(1.0, env * gain)
    if e <= 1e-6:
        return 0
    db = 20.0 * math.log10(e)                 # -inf..0
    f = max(0.0, (db + FLOOR_DB) / FLOOR_DB)  # -FLOOR_DB..0 dB -> 0..1
    return max(0, min(LMAX, int(f * LMAX)))
