# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- Release flow: accumulate entries under [Unreleased]; at release time rename it to
     "## [X.Y.Z] - YYYY-MM-DD" and tag vX.Y.Z. The release workflow pulls that section
     into the GitHub release notes automatically (packaging/changelog-section.sh). -->

## [Unreleased]

## [1.1.1] - 2026-06-23

### Added
- **AppImage auto-update** — the AppImage embeds update-information for the GitHub-releases zsync
  transport and ships a `.zsync` asset, so `AppImageUpdate` / `appimageupdatetool` delta-updates
  it in place from the latest release. The tray gains a **Check for updates…** item (AppImage runs
  only) that invokes the updater if installed, else opens the Releases page. Takes effect for
  releases built from this version onward.

## [1.1.0] - 2026-06-23

### Added
- **Device-less tray idle + bidirectional hotplug** — with no device attached the daemon idles
  in the tray with all functionality off (no audio worker / input / metering / lane matching);
  it brings a full session up on attach, tears it down on removal, and resumes on replug.
- **Greyed tray icon while idle** — the tray icon dims while no device is served and returns to
  normal once a session is active.
- **Crash logging** — `faulthandler` dumps every thread's stack to `logs/crash.log` on a fatal
  signal, so a C-level abort self-records instead of dying silently.

### Fixed
- **Surprise removal no longer aborts the process** — an isochronous write to a just-removed
  device made pyusb feed libusb a negative iso-packet count, triggering an uncatchable `SIGABRT`.
  `Scheduler.send` now gates every isoc write on the device's usbfs node still existing.
- A device-session error now drops to tray idle instead of killing the daemon.

### Changed
- Removed the tray on/off toggle from the config editor (the `tray` key stays in `config.toml`
  and is still honored).
- Documentation restructured into per-component sub-docs under `docs/` (`PROTOCOL`, `CODECS`,
  `DEVICE-IO`, `DAEMON`, `STREAM200`) with a slimmed CLAUDE.md hub and a `src/` module map.

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

[Unreleased]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cont1nuity/hercules-stream-linux/releases/tag/v1.0.0
