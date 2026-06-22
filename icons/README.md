# icons/ — display icons selectable in config.toml

These are the images the `icons` / `button_icons` fields in `config.toml` can reference.
They are rendered to the device's 32x32 icon slots by the display daemon (`src/ui.py`).

This is an ORIGINAL icon set drawn for this project (simple white-on-transparent SVG,
24x24 viewBox) — no vendor assets, freely redistributable with the project.

## The set

| name | use | name | use |
|---|---|---|---|
| `volume` / `volume-mute` | speaker / muted ("default" lane, mute buttons) | `mic` / `mic-mute` | microphone lanes |
| `browser` | web browser lane | `music` | music-player lane |
| `voice` | headset — voice-chat lane (Discord etc.) | `chat` | text chat |
| `game` | gamepad | `video` | video player |
| `monitor` | desktop/system | `apps` | generic app |
| `arrow-right` / `arrow-left` | `page:next` / `page:prev` buttons | `gear` | settings page |
| `record` / `stream` | recording / broadcast action buttons | `hercules` | app logo (H over three pyramids) — tray/desktop icon, usable on lanes too |

## How to reference an icon

Two forms. A bare NAME = file name without extension from this folder, case-insensitive:

```toml
icons        = ["voice", "browser", "mic", "volume"]
button_icons = ["volume-mute", "", "", "arrow-right"]   # "" = empty slot
```

Or an explicit FILE PATH (anything containing a `/`) for custom icons kept anywhere —
absolute, `~`, `$VAR`, and repo-root-relative all work:

```toml
icons = ["~/Pictures/icons/krita.png", "/opt/art/cat.svg", "icons/voice.svg", "volume"]
```

Unresolvable names/paths print a warning at startup and leave the slot empty.

## Formats

- `.svg` — rasterized at 32x32 via `rsvg-convert` (librsvg must be installed)
- `.png` / other raster — loaded via PIL, scaled to fit 32x32, centered

Pure black and transparent pixels become the panel background; everything else is drawn
as-is (RGB565). Light/white glyphs on transparency look best on the dark panel theme.

## Adding your own (incl. brand icons)

Drop any `.svg`/`.png` here and reference its stem in config — 32x32 (or 24x24 vector)
with transparent background is the sweet spot. Brand icons (Discord, Spotify, ...) are NOT
shipped for licensing reasons; you may add ones you are entitled to use yourself.
Label font: Noto Sans in `../fonts/` (OFL license).
