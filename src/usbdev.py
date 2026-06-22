"""Device discovery with explicit libusb selection.

Packaged runs (AppImage) ship their own libusb-1.0 and pin it via the HERCULES_STREAM_LIBUSB
env var (set by AppRun): an absolute .so path handed to pyusb's libusb1 backend. Plain
checkouts leave the env unset and pyusb locates the host library as usual.

This is kept separate from LD_LIBRARY_PATH on purpose: the daemon spawns host tools
(pactl/parec), and exporting the AppImage's lib dir to them would shadow host libraries.
"""
import os


def find(**kwargs):
    """usb.core.find(...), with the pinned backend when HERCULES_STREAM_LIBUSB is set."""
    import usb.core
    lib = os.environ.get("HERCULES_STREAM_LIBUSB")
    if lib and os.path.isfile(lib) and "backend" not in kwargs:
        import usb.backend.libusb1
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name: lib)
        if backend is not None:
            kwargs["backend"] = backend
    return usb.core.find(**kwargs)
