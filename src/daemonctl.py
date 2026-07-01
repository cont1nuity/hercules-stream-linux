"""Locate and restart the running daemon — used by the config editor's "Apply & Restart".

Dependency-free (stdlib + paths). The daemon's single-instance lock file holds its pid
(ui.single_instance() truncates it and writes os.getpid()), so we can find a running daemon
from any process in the same session — whether the editor was launched from the tray or
standalone — and restart it the same way the tray does.
"""
import os
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths


def lockfile():
    """The single-instance lock path — mirrors ui.single_instance()."""
    rt = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(rt, "hercules-stream.lock")


def daemon_pid():
    """PID of the running daemon (from the lock file) if it is alive, else None."""
    try:
        with open(lockfile()) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def launch_cmd():
    """What to (re)launch — resolved in paths.install_target() (mirrors tray.launch_cmd()): our
    installed AppImage copy (a relocated download), the adopted $APPIMAGE (a tool-placed one), or
    the repo checkout's start.sh."""
    return paths.install_target()


def restart_daemon(pid=None):
    """Restart the daemon: spawn a DETACHED relauncher that waits for the daemon to exit
    (releasing its single-instance flock) and then execs the launch command, and SIGTERM the
    daemon for a clean shutdown. Returns the restarted pid, or None if no daemon is running.

    Mirrors tray._restart(): single_instance() takes the flock NON-blocking, so a fresh daemon
    started before the old one releases it would just bail — hence the wait-for-exit."""
    if pid is None:
        pid = daemon_pid()
    if not pid:
        return None
    cmd = launch_cmd()
    waiter = ('i=0; while kill -0 "$DPID" 2>/dev/null && [ "$i" -lt 75 ]; do '
              'sleep 0.2; i=$((i+1)); done; exec "$CMD"')
    try:
        subprocess.Popen(["/bin/sh", "-c", waiter], start_new_session=True,
                         env={**os.environ, "DPID": str(pid), "CMD": cmd},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    return pid
