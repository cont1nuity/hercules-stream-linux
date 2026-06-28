# Hercules Stream for Linux

A Linux driver/daemon for the Hercules **Stream** family of audio controllers (Guillemot
Corp.), built by reverse-engineering the Windows-only *Hercules Stream Control* app.
Currently supported hardware: the **Hercules Stream 100** (USB `06f8:e053`).

Knobs and buttons drive **per-app PipeWire volume/mute**; the built-in 4.3" LCD shows pages
of icons, labels and volume arcs, with **live per-lane VU meters**. Both protocol halves
(input and display) are fully decoded — no Windows software involved, ever.

## Features

- **Per-app volume control**: each encoder is mapped to a "lane" — the system default
  output, a microphone, or any application (matched by name, with `|` alternatives and
  aliases like `"browser"` / `"game"`). Turn = volume, push = mute.
- **Live display**: per-lane icon + label + volume arc, live VU bars metering each lane's
  *own* audio, page switching, brightness control, button LEDs.
- **Action buttons**: each page maps the four buttons to mute toggles, page switching, or
  arbitrary shell commands.
- **Pages**: as many as you like, four lanes each, freely configured in a single TOML file.
- **VU colors**: per-lane bar color or multi-stop gradient (the firmware blends), plus a
  configurable warning band, clip zone, peak cap, and background.
- **Icons**: a built-in redistributable icon set (`icons/`), or point any lane at your own
  image file.
- **Graphical config editor**: a built-in settings window (system tray → *Configure…*, or
  `python3 src/configui.py`) with a **live preview** of the panel, per-page lane/button
  editing, icon/colour pickers, add/remove pages, and **comment-preserving** save — plus
  *Apply & Restart* to push changes to the device. Pure Tkinter (stdlib), bundled in the
  AppImage; falls back to plain-file editing if Tk isn't available.
- **System tray** (optional): status icon that opens the config editor, with a start-at-login
  toggle and quit — implemented directly over D-Bus, no Qt/GTK dependency.
- **Self-contained AppImage**: bundles Python and all libraries; the host only needs
  PipeWire (`pactl`/`parec`) and a udev rule, which the first-run setup offers to install
  for you.

## Install

### AppImage (recommended)

Download the latest `Hercules-Stream-Linux-*-x86_64.AppImage` from the releases page,
make it executable, and run it:

```sh
chmod +x Hercules-Stream-Linux-*-x86_64.AppImage
./Hercules-Stream-Linux-*-x86_64.AppImage
```

On first run a preflight checks your system and offers to fix what's missing: the
PulseAudio client tools (`pactl`/`parec`) if absent, a config file created from the
example, and the udev rule that grants your user access to the device (installed via a
native polkit prompt; replug the device afterwards when asked).

#### Steam Deck / Steam Machine

Both run on x86-64 AMD APUs, so the standard `x86_64` AppImage is the right build — no
separate ARM/aarch64 download. (Of Valve's current hardware only the Steam Frame VR
headset is ARM, and it isn't a target for a USB desktop mixer.) The first-run preflight
also handles SteamOS's read-only rootfs automatically: when it installs the udev rule it
runs `steamos-readonly disable`, copies the rule, then re-locks the filesystem — all
behind the one polkit password prompt. If a major SteamOS update later wipes the rule,
just start the app again and it re-offers the install.

Config lives at `~/.config/hercules-stream/config.toml`, logs at
`~/.local/state/hercules-stream/`.

### Updates

The tray checks GitHub on startup (and periodically) and notifies you when a newer release
is out — toggle it via the tray's *Check for updates automatically* item or `[ui] check_updates`
in the config. The notification and the *Check for updates…* tray item then do an **in-place
delta update** *only if* [AppImageUpdate](https://github.com/AppImageCommunity/AppImageUpdate)
is installed; otherwise they just open the Releases page so you can download the new AppImage
manually. (The AppImage carries the embedded update-information AppImageUpdate needs — no
extra config.)

To get one-click updates, put AppImageUpdate on your `PATH` as `appimageupdatetool` or
`AppImageUpdate` — download `appimageupdatetool-x86_64.AppImage` from its
[releases](https://github.com/AppImageCommunity/AppImageUpdate/releases) (`chmod +x`, drop it
in `~/.local/bin/appimageupdatetool`), or install your distro's package (Arch AUR:
`appimageupdate`). It is **not bundled**: it would add weight and a vendored binary to keep
current, and the manual-download fallback already works — install it once if you want hands-off
updates.

### From source

Python 3.9+ (3.11+ uses the stdlib `tomllib`; on older it auto-installs `tomli`), `libusb-1.0`,
and PipeWire with `pactl`/`parec` (`rsvg-convert` for the SVG icons; on Arch: `python-dbus-next`
for the tray). The AppImage bundles its own Python 3.12, so it needs none of this on the host.

```sh
./setup.sh     # venv (pyusb, pillow), udev rule (sudo once), config.toml from example
./start.sh     # run the daemon
```

A source checkout uses the repo-local `config.toml` and `logs/`.

## Configuration

The easiest way is the **graphical editor** — system tray → *Configure…*, or run
`python3 src/configui.py` — which edits pages, lanes, buttons, icons and colours with a live
panel preview, keeps your comments on save, and can restart the daemon to apply. To edit by
hand instead, copy `config.example.toml` to `config.toml` (setup does this) and edit the
lanes. Find the match strings for your applications with:

```sh
python3 src/stream100.py list    # lists current PipeWire streams with app names
```

Lane targets: `"default"` (system output), `"mic"` (default input), `"mic:<name>"` (a named
capture source), an application name (`"spotify"`, `"vlc|mpv"`, …), or the aliases
`"browser"` / `"game"`. Button actions: `mute:<lane>`, `page:next|prev|<n>`, `cmd:<shell>`,
`none`. Per-page `icons`, `labels` and `colors` are optional (a `colors` entry may be a
list of stops for a gradient); the `[ui]` section holds knobs like encoder sensitivity,
VU behavior and colors, tray and brightness.

## Usage

Run the AppImage or `./start.sh`. The panel wakes, shows page 1, and follows the hardware
from there — encoders set volume (capped at 100%), pushes toggle mute, the page button
switches pages. Volumes are OS-authoritative: external changes (e.g. from a desktop mixer)
are re-read and shown on the panel.

Useful flags: `--debug` (trace logging), `--no-preflight` (skip first-run checks),
`--selftest` (offline frame validation, no hardware needed).

Other entry points:

```sh
python3 src/stream100.py probe          # watch raw input events (knob/button byte-diffs)
python3 src/stream100.py run -c config.toml   # input-only mode, no display
python3 src/display.py --image x.png    # show a 480×272 image on the panel
```

## Hardware & protocol

The device is a vendor-specific USB control surface (not HID, not a sound card): 4 push
encoders, 4 action buttons, 2 pages, and a 480×272 LCD fed over an isochronous endpoint.
All mixing is host-side — the device only sends input and renders what it's told.

The full reverse-engineered wire format (input report, display frame/CRC format, op
grammar, image codec) is documented in [CLAUDE.md](CLAUDE.md), which doubles as the
contributor guide. The protocol rules there are non-negotiable — in particular: **no
guessed protocol bytes**, every frame CRC'd, nothing blocking in the display cadence loop,
and the daemon never changes system audio except in response to user input.

```
06f8:e053  Guillemot Corp. "Stream 100"  (USB 1.1 Full Speed, 2 vendor interfaces)
  IF0  EP 0x02 bulk OUT       host→device commands
       EP 0x81 interrupt IN   input events (encoders, buttons, pages)
  IF1  EP 0x01 isoc OUT       LCD stream (952-byte packets)
```

## Status

v1 is feature-complete and hardware-verified. Remaining work is release plumbing (CI,
published releases). If something misbehaves, check the newest log in
`~/.local/state/hercules-stream/` (or `logs/` in a source checkout) and open an issue —
please include the log and your `config.toml`.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). This covers all code, docs, and the original
icon set in `icons/`. The Noto Sans fonts in `fonts/` are by the Noto Project Authors under
the SIL Open Font License 1.1 (`fonts/OFL.txt`).

Hercules and Stream 100 are trademarks of Guillemot Corporation S.A. This project is not
affiliated with or endorsed by Guillemot; it interoperates with their hardware.
