# docs/assets

This directory holds visual assets for the README and documentation.

## demo/

Rendered from the committed demo player at `demo/index.html` (light mode, English, fully scripted — no live backend needed) via Playwright (Chromium) + ffmpeg.

| File | Description | Used in |
|---|---|---|
| `demo/demo.gif` | Looping screen recording of the scripted discovery call: live transcript, pain points firing with a matched case slide, the monologue coaching alert with the talk-time split, a price objection with counter-response, and a buying signal. Palette-optimized, ~4.5 MB. | `README.md` Demo section |
| `demo/screenshot-dashboard.png` | Static screenshot with the monologue coaching alert and talk-time split visible. | `README.md` Demo section |
| `demo/screenshot-objection.png` | Static screenshot with a detected price objection and its counter-response suggestion visible. | `README.md` Demo section |

## How to re-render them

1. Confirm tooling: `python3 -c "import playwright"` (install the browser with `python3 -m playwright install chromium` if missing) and `which ffmpeg` (`brew install ffmpeg` if missing).
2. Open `demo/index.html` directly (`file://`) in Playwright Chromium, viewport ~1280x800, light color scheme — the demo shim forces English copy and the light theme itself, no query params needed.
3. Record video for the full run (`demo/demo-timeline.js` drives ~57s of scripted events) and grab PNG screenshots at the moments you want (e.g. around the monologue coaching alert, around the price objection).
4. Encode the GIF with ffmpeg's two-pass palette workflow (`palettegen` / `paletteuse`), speeding up playback (`setpts`) and trimming the window as needed to keep the file lean — target ~960px wide, ~8-12 fps, ≤ ~5 MB.

Do not commit raw video files or large uncompressed images. Optimize the GIF before adding it.
