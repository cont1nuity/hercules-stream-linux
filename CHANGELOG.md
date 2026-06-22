# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- Release flow: accumulate entries under [Unreleased]; at release time rename it to
     "## [X.Y.Z] - YYYY-MM-DD" and tag vX.Y.Z. The release workflow pulls that section
     into the GitHub release notes automatically (packaging/changelog-section.sh). -->

## [Unreleased]

## [1.0.0] - 2026-06-22

First release. Full support for the Hercules **Stream 100** (USB `06f8:e053`),
reverse-engineered from the Windows *Hercules Stream Control* app.

### Added
- **Input** — 4 encoders (turn + push-to-mute) and 4 action buttons decoded from the
  HID stream, with multi-page navigation.
- **LCD panel (480×272)** — backgrounds, 32×32 icons, text labels, per-lane VU bars,
  volume arcs, brightness, and channel LED states; every frame built from intent and
  CRC-gated before send.
- **Audio** — per-app PipeWire volume/mute driven by the knobs (OS-authoritative:
  set → read back → display, capped at 100%); read-only per-lane VU metering via `parec`
  taps with instant-attack/hold/fall ballistics.
- **Pages & lanes** — configurable `[[pages]]` with lane match strings (`default`, `mic`,
  app names, `|` alternatives, aliases), icons (built-in 18-icon set or any image by path),
  labels, and per-lane colors/gradients.
- **Graphical config editor** (`src/configui.py`) — live 480×272 preview, per-page
  lane/button editing, icon/match/color pickers, comment-preserving TOML save, and
  Apply & Restart.
- **System integration** — system tray (SNI/DBusMenu), login autostart (opt-out),
  single-instance lock, and a first-run udev/setup flow.
- **AppImage packaging** — self-contained build (bundled CPython + deps) via
  `packaging/build-appimage.sh`, published automatically on tagged releases.
- **Stream 200 XLR** (`06f8:e054`) — experimental backend behind a feature flag
  (off by default): telemetry-driven audio control; on-panel display not yet implemented.

[Unreleased]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/cont1nuity/hercules-stream-linux/releases/tag/v1.0.0
