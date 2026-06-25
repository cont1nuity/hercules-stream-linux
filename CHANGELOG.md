# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- Release flow: accumulate entries under [Unreleased]; at release time rename it to
     "## [X.Y.Z] - YYYY-MM-DD" and tag vX.Y.Z. The release workflow pulls that section
     into the GitHub release notes automatically (packaging/changelog-section.sh). -->

## [Unreleased]

## [1.2.2] - 2026-06-25

### Fixed
- **Startup update check could miss a release** — the check fired exactly once at tray boot and
  silently swallowed any failure, so if the network wasn't ready at that instant (login/resume race)
  the notification never appeared for the life of the daemon (days, for the hotplug daemon). It now
  waits a short settle delay, retries on failure (logging the reason to `tray.err` instead of
  vanishing), and re-checks periodically so a release published while the daemon idles still
  surfaces — each newer version announced once per session.

### Changed
- **README** now documents that one-click *in-place* updates require
  [AppImageUpdate](https://github.com/AppImageCommunity/AppImageUpdate) on `PATH` (otherwise the
  tray/notification just open the Releases page to download manually), how to install it, and why
  it isn't bundled.

## [1.2.1] - 2026-06-25

### Fixed
- **VU meter could go permanently flat for a lane** — meters are `parec` taps that are only
  respawned when a lane's binding *key* changes. App lanes self-heal because their sink-input
  index churns, but a device lane (mic → `@DEFAULT_SOURCE@`, master → `@DEFAULT_MONITOR@`) has a
  key that never changes, so if its tap ever died — e.g. the source was momentarily unresolved
  when the tap was spawned (observed with a virtual-audio-cable reconfiguring the default
  source) — it was never detected, reaped, or respawned and read silence forever. The 2 s
  re-sync now checks each tap's liveness (`Meter.dead()`) and drops a dead binding so it gets
  respawned; this also reaps the orphaned `parec` and recovers any stale app tap.

## [1.2.0] - 2026-06-24

### Added
- **Config hot-reload** — editing `config.toml` (by hand or via the config editor) now applies
  live without a restart: the daemon watches the file's mtime (on the existing 1 Hz off-cadence
  poll) and, on change, rebuilds the config and bounces the device session (brief panel blink; the
  process, tray, and lock survive). A broken edit (bad TOML, no `[[pages]]`) is rejected and the
  running config kept.

## [1.1.2] - 2026-06-23

### Added
- **Update notification** — on startup the tray checks GitHub for a newer release (AppImage runs
  only) without using the API (a `HEAD` on `/releases/latest`, no token/rate-limit) and, if one
  exists, shows a desktop notification and relabels its menu item to "Update available: vX.Y.Z".
  Toggle it via the tray's **Check for updates automatically** checkmark or `[ui] check_updates`
  (default on) on the config editor's Display tab.

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

[Unreleased]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.1.2...HEAD
[1.1.2]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/cont1nuity/hercules-stream-linux/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cont1nuity/hercules-stream-linux/releases/tag/v1.0.0
