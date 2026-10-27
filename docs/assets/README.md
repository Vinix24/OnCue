# docs/assets

This directory holds visual assets for the README and documentation.

## demo/

Rendered from the committed demo player at `demo/index.html` (light mode, English, fully scripted — no live backend needed) via Playwright (Chromium) + ffmpeg.

| File | Description | Used in |
|---|---|---|
| `demo/demo-notetaker.gif` | Looping screen recording of the notetaker preset (English): the live transcript of both sides as "Me" and "Other", no detection and no talk-time coaching, then Stop & report, the post-call report view and the Markdown download that ends with the transcript. Palette-optimized, ~4.6 MB. | `README.md` Notetaker section |
| `demo/demo-notetaker-nl.gif` | The same recording in Dutch ("Ik" and "Gesprekspartner"). Palette-optimized, ~4.5 MB. | Not linked from the README |
| `demo/demo.gif` | Looping screen recording of the scripted discovery call in the live copilot: live transcript, pain points, the monologue coaching alert with the talk-time split, a price objection with counter-response, and a buying signal. Palette-optimized, ~4.5 MB. | `README.md` Demo section |
| `demo/screenshot-dashboard.png` | Static screenshot with the monologue coaching alert and talk-time split visible. | `README.md` Demo section |
| `demo/screenshot-objection.png` | Static screenshot with a detected price objection and its counter-response suggestion visible. | `README.md` Demo section |
| `demo/screenshot-objection-nl.png` | The price-objection screenshot in Dutch, 1280x800 at device scale factor 3. | Not linked from the README |

The demo player shows no slide UI in any timeline. It is the demo player only: `dashboard/` is unchanged.

## How to re-render them

1. Confirm tooling: `python3 -c "import playwright"` (install the browser with `python3 -m playwright install chromium` if missing) and `which ffmpeg gifsicle` (`brew install ffmpeg gifsicle` if missing).
2. Open `demo/index.html` directly (`file://`) in Playwright Chromium, viewport 1280x800, light color scheme. The demo shim forces the light theme itself. Query parameters pick the timeline:

   | URL | Timeline | Runs |
   |---|---|---|
   | `demo/index.html` | `demo/demo-timeline.js` | ~57s live copilot, English |
   | `demo/index.html?lang=nl` | `demo/demo-timeline-nl.js` | live copilot, Dutch |
   | `demo/index.html?mode=notetaker` | `demo/demo-timeline-notetaker.js` | ~64s notetaker, English |
   | `demo/index.html?mode=notetaker&lang=nl` | `demo/demo-timeline-notetaker-nl.js` | notetaker, Dutch |

   The notetaker timeline serves the `notetaker` preset from the shim, hides the coaching rail, and ends with Stop & report, the report view and the Markdown download shown in a preview panel.
3. Record video for the full run and keep recording for about 15 seconds after the last scripted event, so the end state holds. Screen-capture video runs slower than the page clock (about 1.2x in the last render), so check where the report view appears before choosing the trim window. For the static screenshots, take PNGs at the moments you want (about 26s for the monologue alert, about 41s for the price objection; use `device_scale_factor=3` for the Dutch one).
4. Encode the GIF with ffmpeg's two-pass palette workflow (`palettegen` / `paletteuse`), speeding up playback (`setpts`) and trimming the window as needed. Target about 960px wide, 8-12 fps, at most ~5 MB. What worked for the current files: `fps=8`, speed 1.5x, `palettegen=max_colors=64:stats_mode=diff`, then `gifsicle -O3 --lossy=130`. The live copilot recording is noisier (the VP8 video compression noise costs bytes): add `hqdn3d=7:5:12:9` before the scale and use `dither=none`.
5. Extract a few frames from each GIF (`ffmpeg -ss <s> -i x.gif -frames:v 1 f.png`) and check them: no slide UI, readable text, no broken layout.

Do not commit raw video files or large uncompressed images. Optimize the GIF before adding it.
