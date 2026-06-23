# Runtime, packaging & UI

Operational reference for Hercules Stream for Linux — config/log locations, the AppImage
build, first-run setup, the single-instance lock, the system tray, and the graphical config
editor. The non-negotiable protocol **rules** live in [../CLAUDE.md](../CLAUDE.md) and the **wire
format** in [PROTOCOL.md](PROTOCOL.md); this file is the on-demand detail for the runtime/UI side.

## Config & log homes

A dev checkout uses repo-local `config.toml` + `logs/`. Packaged runs (`APPDIR` set by the
AppImage runtime, or repo root read-only) read `~/.config/hercules-stream/config.toml` and log
to `~/.local/state/hercules-stream/` (`paths.config_path()` / `paths.LOGS` — `src/paths.py`
owns the switch). New scripts resolve data dirs through `paths`; don't hardcode paths.

## AppImage (`packaging/build-appimage.sh`)

Self-contained — bundles CPython (python-appimage manylinux build) + pyusb + Pillow + dbus-next
+ libusb-1.0, the `icons/` set pre-rendered to 32×32 PNG (no runtime `rsvg-convert` needed),
`fonts/`, the udev rule and `config.example.toml`. Target host needs only `pactl`/`parec` + the
udev rule. The bundled libusb is pinned via the `HERCULES_STREAM_LIBUSB` env var
(`src/usbdev.py`), deliberately NOT via `LD_LIBRARY_PATH` — spawned host tools (`pactl`/`parec`)
must keep resolving host libraries. AppRun must exec the python ELF under `opt/` (the `usr/bin`
python in python-appimage is a bash wrapper that would linger as a second process). The bundled
CPython already ships Tkinter + Tcl/Tk, so AppRun re-exports `TCL_LIBRARY`/`TK_LIBRARY`/`TKPATH`
(detected at build time) — the config editor runs from the AppImage too, at no size cost. Build
host needs bash, curl, rsvg-convert; downloads cache in `build/cache/`; release builds should
run on the oldest supported distro (libusb is copied from the build host). The build stamps
`VERSION` in the payload's `ui.py`.

**Auto-update.** The build also embeds AppImage **update-information** — the `gh-releases-zsync`
transport pointing at this repo's `latest` release — and appimagetool writes a `.zsync` file next
to the AppImage, so `AppImageUpdate` / `appimageupdatetool` can delta-update an installed AppImage
in place. The release workflow installs `zsync` (for `zsyncmake`) and uploads the `.zsync` asset
beside the AppImage; a local build without `zsync` still embeds the update-info but skips the
`.zsync`. The tray's **Check for updates…** item (AppImage runs only) drives the install — see the
tray section below.

On tray startup (AppImage runs only, honoring `[ui] check_updates`, default on) a lightweight check
runs off the dbus loop: a `HEAD` on `…/releases/latest` follows the 302 to `…/tag/vX.Y.Z`, so it
reads the newest tag **without the GitHub API** (no token, no rate limit). If that's newer than the
running `--version`, the tray fires a desktop notification (`notify-send`, skipped if libnotify is
absent) and relabels its item to **Update available: vX.Y.Z — install now**. The check is
best-effort — any network/SSL failure just no-ops. It is toggled by `[ui] check_updates`, editable
two ways (kept in sync, single source = `config.toml`): the tray's **Check for updates
automatically** checkmark and the config editor's Display tab.

## Build overrides & feature flags (`src/features.py`)

Some settings are resolved through a three-layer chain — **highest wins**:

    src/build_overrides.toml   >   config.toml   >   code defaults

`src/features.py` owns it. `apply_overrides(cfg)` deep-merges an optional, build-baked TOML
overlay (`src/build_overrides.toml`, inside the read-only payload) on top of the user's config —
overrides win; nested tables merge, scalars/arrays replace. `enabled(cfg, name)` resolves a named
**feature flag** against the overlay, then the user config's `[features]` table, then
`FEATURE_DEFAULTS` (code defaults). The overlay is optional: absent or malformed → "no overrides,
defaults apply", never a hard failure. A stock dev checkout has no overlay.

Bake an overlay at build time with `packaging/build-appimage.sh [VERSION] [--set KEY=VALUE …]`:

- `--set` takes a dotted key; the value is parsed as bool/int/float, else string. **Repeatable** —
  every `--set` is honored; keys in the same section merge into one TOML table; the same key twice
  is last-wins.
- `--enable-stream200` is shorthand for `--set features.stream200=true`, and composes with other
  `--set` flags in one build.
- Example: `build-appimage.sh 1.0.0 --enable-stream200 --set ui.brightness=80`.
- Stock public builds pass none (and strip any dev-local overlay), so every flag resolves to its
  code default. The overlay file is hidden — it is **not** part of `config.example.toml`.

**Feature flag: `stream200`** (default **OFF**) gates the experimental Stream 200 XLR backend.
With it off, the daemon refuses a detected 200 with a hint. Enable it either per-user
(`[features] stream200 = true` in `config.toml`) or per-build (`--enable-stream200`, which
overrides config — used for tester builds). The config editor's **200 XLR** tab honors the same
flag (the `[stream200] show_tab` dev override still forces the tab regardless).

## First-run preflight (`src/firstrun.py`)

Runs on every daemon start (`--no-preflight` skips). Before touching the device, checks host
tools (`pactl`/`parec` — offers a pkexec install of the distro's client-tools package,
pacman/apt-get/dnf/zypper detected, `libpulse` / `pulseaudio-utils`; manual hint on SteamOS or
unknown distros), offers to create the live config from `config.example.toml`, and sorts out
device access — "not plugged in" no longer blocks or prompts: preflight returns `None` and the
daemon idles in the tray (everything off) until a device is hotplugged (see the hotplug supervisor
in the Architecture notes). An EACCES on the USB node still gets an offer to install the udev rule
via **pkexec** (native polkit password dialog; the root helper + rule are copied to a temp dir
first because root cannot read inside the AppImage's FUSE mount), then a wait-for-access /
replug-retry loop, and the daemon continues. Dialogs are kdialog/zenity
(detected), console prompts on a TTY, or a non-interactive fallback (auto-creates the config,
fails loud on blockers) so login-autostart runs self-heal. The config carries a `config_version`
marker (currently 1, `CONFIG_VERSION` in `ui.py`); missing keys always take built-in defaults at
startup, so the marker only gates future meaning-changes of existing keys.

## Single instance

`ui.py` takes an exclusive flock on `$XDG_RUNTIME_DIR/hercules-stream.lock` at startup (repo
checkout and AppImage contend on the same file) and writes its pid there; a second launch exits
with the owner's pid. `src/daemonctl.py` reads that pid to find/restart the running daemon.

## System tray (`src/tray.py`, `[ui] tray = true`)

StatusNotifierItem + DBusMenu implemented directly over D-Bus with `dbus-next` (pure python —
no Qt/GTK, keeps the AppImage small). Runs as a separate process spawned by the daemon (the
20 ms slot loop must never host an event loop), dies with it (PDEATHSIG + pid poll), and exits
silently if dbus-next or the session bus is missing — daemon runs fine trayless (repo runs:
`pacman -S python-dbus-next`). Left-click opens the **graphical config editor**
(`src/configui.py`, below); the menu shows version, **Configure…** (the editor) and **Edit
config file** (raw `xdg-open`, also the fallback when Tk/PIL are unavailable), a **Start at
login** checkbox (XDG autostart entry `~/.config/autostart/hercules-stream.desktop`, default ON,
rewritten each start so Exec tracks AppImage vs `start.sh`; opt-out remembered via a marker in
the XDG state dir), a **Check for updates automatically** checkmark and a **Check for updates…**
item (both AppImage runs only — the checkmark toggles `[ui] check_updates`; the item hands off to
`appimageupdatetool` / `AppImageUpdate` if installed, else opens the Releases page; see "Auto-update"
above), **Restart** (relaunch the daemon to apply config edits — spawns a detached
relauncher that waits for the current daemon to exit and release the single-instance flock, then
execs `launch_cmd()`, since `single_instance()` takes the flock non-blocking), and Quit
(SIGTERM → clean shutdown). Tray stderr lands in `<logs>/tray.err`. While the daemon is **idle**
(no device attached) the tray **greys/dims its icon**, switching back to the normal icon while a
device is served: the daemon publishes `idle`/`active` to `$XDG_RUNTIME_DIR/hercules-stream.state`
(passed as `--state-file`) and the tray polls it (~1 s). The `tray` on/off setting lives only in
`config.toml` now — the config editor no longer shows a checkbox for it.

## Config editor (`src/configui.py`, Tkinter)

A standalone GUI launched from the tray (spawned with `sys.executable`, single-instance,
stderr → `<logs>/configui.err`) or run directly (`python3 src/configui.py [--config p]`). A tab
per page edits lanes (match/icon/label/VU colour) and action buttons (a main/sub dropdown pair),
a **Display** tab holds the `[ui]` knobs, and a **live 480×272 preview** re-renders from the
daemon's own element path (`Renderer` + `codec_element.render`) — icons/labels/colours are exact,
the *arrangement* is approximate (firmware hardcodes positions). Add/remove pages; pickers for
icons (built-ins + Browse) and matches (live `pactl` apps + tokens); a colour-wheel swatch.
**Save is comment-preserving**: `merge_into_toml()` rewrites only changed values in the original
text (keeping comments/alignment), verified by reparse against `toml_dump()` and falling back to
it if values would differ — so values are never corrupted, only comments lost in edge cases; a
`.bak` is always written. Bottom-row buttons: **Revert** reloads from disk; **Apply (save)**
writes the file; **Apply & Restart** also restarts the daemon via `src/daemonctl.py` (finds the
daemon pid in the single-instance lock file, spawns the same detached relauncher the tray uses);
**Close** (bottom-right) dismisses the window. Tkinter needs Tk; the tray's launch gate falls back to
raw-file editing when Tk/PIL are missing. The AppImage already bundles `_tkinter` + Tcl/Tk
(python-appimage ships them) — `AppRun` re-exports `TCL_LIBRARY`/`TK_LIBRARY` so the editor runs
from the AppImage too.
