#!/usr/bin/env bash
# Setup for Hercules Stream for Linux (Stream 100; Stream 200 XLR is experimental).
# Works on SteamOS (immutable rootfs) and normal distros. Run as your normal user.
# The udev rule installed below covers the whole family (e053/e054/e055).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 1) Python venv + deps. Home is writable even on SteamOS, so no system pip / pacman needed.
#    pyusb = USB, pillow = icon/label rendering, dbus-next = system tray (config editor is
#    launched from it); tomli only for Python < 3.11. (System dep for SVG icons: rsvg-convert
#    from librsvg. The graphical config editor also needs Tk — Python's stdlib tkinter, e.g.
#    `pacman -S tk` / `apt install python3-tk`; it degrades to raw-file editing without it.)
echo ">> creating venv + installing pyusb + pillow + dbus-next"
python3 -m venv .venv
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet "pyusb>=1.2" pillow dbus-next 'tomli>=2.0; python_version < "3.11"'
echo "   venv ready at $DIR/.venv"

# 2) live config: create from the example on first install; ask before overwriting.
if [ -f config.toml ]; then
  read -r -p ">> config.toml already exists — overwrite with config.example.toml? [y/N] " ans
  case "$ans" in
    [yY]*) cp config.example.toml config.toml; echo "   overwritten." ;;
    *)     echo "   keeping your config.toml." ;;
  esac
else
  cp config.example.toml config.toml
  echo ">> created config.toml from config.example.toml — edit lanes/icons to taste"
fi

# 3) udev rule for non-root libusb access (uaccess -> active KDE/SteamOS user gets an ACL).
RULE=99-hercules-stream.rules
STEAMOS=0
command -v steamos-readonly >/dev/null 2>&1 && STEAMOS=1

echo ">> installing udev rule (needs sudo)"
if [ "$STEAMOS" = 1 ]; then
  echo "   SteamOS: set a sudo password first if you haven't ('passwd'). Disabling readonly..."
  sudo steamos-readonly disable
fi
sudo cp "$RULE" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
[ "$STEAMOS" = 1 ] && sudo steamos-readonly enable || true

cat <<EOF

Done. Replug the device, then:
  ./start.sh                          # display + knobs/buttons -> PipeWire (the daemon)
  (edit config.toml to taste — lanes, icons [icons/README.md], colors, [ui])
  start.sh auto-detects a Stream 100 or a Stream 200 XLR.

Input-only / RE tools (venv: . $DIR/.venv/bin/activate):
  python3 src/stream100.py list       # find app names for config
  python3 src/stream100.py probe      # raw input byte view (Stream 100)
Stream 200 XLR (experimental): MAIN dials/buttons work via [[pages]] like the 100;
the extra right-side controls need a one-time hardware mapping not automated yet
(see docs/STATUS.md), then configure them in the config editor's "200 XLR" tab.

Note (SteamOS): a major OS update can wipe /etc udev rules — just re-run ./setup.sh.
EOF
