#!/usr/bin/env bash
# Start the Hercules Stream daemon (display + encoders/buttons -> PipeWire).
#
#   ./start.sh                 # uses ./config.toml
#   ./start.sh --debug         # + device-level I/O trace -> logs/ui-debug.log
#   ./start.sh -c other.toml   # alternative config
#
# First-time setup: ./setup.sh (venv + udev rule), then copy config.example.toml to
# config.toml and edit lanes/icons (see icons/README.md for icon names).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/.venv/bin/activate" ]; then
  . "$DIR/.venv/bin/activate"
fi
exec python3 "$DIR/src/ui.py" "$@"
