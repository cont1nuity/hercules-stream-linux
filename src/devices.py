"""Supported-device registry and variant detection — the one place that knows which USB
IDs this project drives and what KIND of device each is.

The project began as a Hercules Stream 100 (06f8:e053) driver. The Stream family shares a
vendor USB design but NOT a wire protocol: the Stream 200 XLR (06f8:e054) speaks a different
transport (bulk on vendor interface 3, no isoc, no SM/CRC framing) and a different input and
display format. So detection cannot be "find the one VID/PID" any more — the daemon and the
CLI tools ask this module what is attached, then dispatch to the matching backend
(`stream100.py` vs `stream200.py`).

Keep this registry THIN: identity (vid/pid), a stable `kind` string the daemon switches on,
and a one-line note. Transport constants (interfaces, endpoints, packet sizes) belong in the
per-device modules, not here.
"""
from collections import namedtuple

VID = 0x06F8

# kind: the backend selector. name: human label. experimental: not yet hardware-verified on
# Linux — the daemon surfaces this so testers know they are on the bring-up path.
Device = namedtuple("Device", "kind name vid pid experimental")

SUPPORTED = (
    Device("stream100", "Hercules Stream 100",          VID, 0xE053, False),
    Device("stream200", "Hercules Stream 200 XLR",      VID, 0xE054, True),
    # e055 = undocumented sibling (HSM02); same vendor transport as e054 per the RE captures.
    Device("stream200", "Hercules Stream 200 XLR (HSM02)", VID, 0xE055, True),
)


def _find():
    """usbdev.find (honors the AppImage bundled-libusb pin); imported lazily so this module
    is importable without pyusb (e.g. for the offline selftests)."""
    import usbdev
    return usbdev.find


def detect(find=None):
    """Return the Device descriptor for the first supported unit currently attached, or None.

    Order follows SUPPORTED (Stream 100 first), so a mixed bench with both plugged in keeps
    the historical default. `find` is injectable for tests; defaults to usbdev.find."""
    if find is None:
        find = _find()
    for d in SUPPORTED:
        try:
            if find(idVendor=d.vid, idProduct=d.pid) is not None:
                return d
        except Exception:
            # A backend hiccup for one id must not hide a different attached device.
            continue
    return None
