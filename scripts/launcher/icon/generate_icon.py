"""Draw the OnCue app icon (concept C: the "O with baton") and its SVG counterparts.

The geometry lives in a 64×64 viewBox and every raster coordinate is this
constant multiplied by ``SCALE``, so the Dock icon and the in-app mark can never
drift apart. On the macOS .icns the tile uses the system squircle radius
(22.5% of the size) instead of the SVG's rx=14.
"""

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

# Pillow is only needed for the raster path; importing this module (for example
# from a test that checks the SVG output) works without it.
if TYPE_CHECKING:
    from PIL import Image, ImageDraw

# Raster master and the design grid it is drawn on.
SIZE = 1024
VIEWBOX = 64
SCALE = SIZE / VIEWBOX
RADIUS = int(SIZE * 0.225)  # macOS squircle
OUT_DIR = Path(__file__).parent
ICONSET_DIR = OUT_DIR / "sales-copilot.iconset"
ICNS_PATH = OUT_DIR / "sales-copilot.icns"
# The dashboard, the presentation view and the demo page all show the same mark,
# so it is emitted as SVG from these same constants instead of being redrawn by
# hand in three places.
_REPO = Path(__file__).resolve().parents[3]
SVG_PATHS = (
    _REPO / "dashboard" / "assets" / "oncue-mark.svg",
    _REPO / "presentation" / "assets" / "oncue-mark.svg",
)

# Concept C: "O with baton". A dark tile, a white ring (the O), a baton that
# crosses it diagonally from the grip, and a red cue dot at the baton's tip.
TILE_COLOR = "#14171f"
MARK_COLOR = "#f5f5f4"
DOT_COLOR = "#ef4444"

TILE_X = 2
TILE_Y = 2
TILE_W = 60
TILE_H = 60
TILE_RX = 14

RING_CX = 29
RING_CY = 36
RING_R = 14
RING_STROKE = 5

BATON_X1 = 24
BATON_Y1 = 41
BATON_X2 = 46
BATON_Y2 = 19
BATON_WIDTH = 3

DOT_CX = 48
DOT_CY = 17
DOT_R = 4.6

ICONSET_SIZES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def hex_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def px(value: float, scale: float = SCALE) -> int:
    return round(value * scale)


def baton_width_for(size: int) -> float:
    """Thicker baton on the smallest rasters, where a 3/64 stroke fades out.

    At 16 px the 3-unit baton is 0.75 px and disappears; 5 units (1.25 px) keeps
    it visible without changing the mark's geometry at normal sizes.
    """
    if size <= 16:
        return 5.0
    if size <= 32:
        return 3.8
    return BATON_WIDTH


def _draw_tile(draw: "ImageDraw.ImageDraw", draw_size: int, scale: float) -> None:
    draw.rounded_rectangle(
        [(px(TILE_X, scale), px(TILE_Y, scale)), (px(TILE_X + TILE_W, scale), px(TILE_Y + TILE_H, scale))],
        radius=round(draw_size * 0.225),  # macOS squircle
        fill=(*hex_rgb(TILE_COLOR), 255),
    )


def _draw_ring(draw: "ImageDraw.ImageDraw", scale: float) -> None:
    outer = RING_R + RING_STROKE / 2
    draw.ellipse(
        [
            (px(RING_CX - outer, scale), px(RING_CY - outer, scale)),
            (px(RING_CX + outer, scale), px(RING_CY + outer, scale)),
        ],
        outline=(*hex_rgb(MARK_COLOR), 255),
        width=px(RING_STROKE, scale),
    )


def _draw_baton(draw: "ImageDraw.ImageDraw", scale: float, width: float) -> None:
    stroke = px(width, scale)
    color = (*hex_rgb(MARK_COLOR), 255)
    draw.line(
        [(px(BATON_X1, scale), px(BATON_Y1, scale)), (px(BATON_X2, scale), px(BATON_Y2, scale))],
        fill=color,
        width=stroke,
    )
    # Round caps: PIL has no cap style, so cover both ends with a disc.
    for cx, cy in ((BATON_X1, BATON_Y1), (BATON_X2, BATON_Y2)):
        draw.ellipse(
            [
                (px(cx, scale) - stroke // 2, px(cy, scale) - stroke // 2),
                (px(cx, scale) + stroke // 2, px(cy, scale) + stroke // 2),
            ],
            fill=color,
        )


def _draw_cue_dot(draw: "ImageDraw.ImageDraw", scale: float) -> None:
    draw.ellipse(
        [
            (px(DOT_CX - DOT_R, scale), px(DOT_CY - DOT_R, scale)),
            (px(DOT_CX + DOT_R, scale), px(DOT_CY + DOT_R, scale)),
        ],
        fill=(*hex_rgb(DOT_COLOR), 255),
    )


def render(size: int, baton_width: float = BATON_WIDTH) -> "Image.Image":
    """Render the mark at ``size`` px, supersampled and then downscaled for clean edges."""
    from PIL import Image, ImageDraw

    supersample = 8 if size <= 32 else 4 if size <= 256 else 2
    draw_size = size * supersample
    scale = draw_size / VIEWBOX
    img = Image.new("RGBA", (draw_size, draw_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_tile(draw, draw_size, scale)
    _draw_ring(draw, scale)
    _draw_baton(draw, scale, baton_width)
    _draw_cue_dot(draw, scale)
    return img.resize((size, size), Image.LANCZOS)


def build_svg() -> str:
    """Return the mark as SVG for the dashboard header, favicon and presentation."""
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEWBOX} {VIEWBOX}" role="img" aria-label="OnCue">',
        f'  <rect x="{TILE_X}" y="{TILE_Y}" width="{TILE_W}" height="{TILE_H}" rx="{TILE_RX}" fill="{TILE_COLOR}" />',
        f'  <circle cx="{RING_CX}" cy="{RING_CY}" r="{RING_R}" fill="none" '
        f'stroke="{MARK_COLOR}" stroke-width="{RING_STROKE}" />',
        f'  <line x1="{BATON_X1}" y1="{BATON_Y1}" x2="{BATON_X2}" y2="{BATON_Y2}" '
        f'stroke="{MARK_COLOR}" stroke-width="{BATON_WIDTH}" stroke-linecap="round" />',
        f'  <circle cx="{DOT_CX}" cy="{DOT_CY}" r="{DOT_R}" fill="{DOT_COLOR}" />',
        "</svg>",
    ]
    return "\n".join(lines) + "\n"


def write_svg() -> None:
    svg = build_svg()
    for path in SVG_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        print(f"Wrote {path}")


def write_iconset() -> None:
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir()
    for name, size in ICONSET_SIZES:
        render(size, baton_width_for(size)).save(ICONSET_DIR / name, "PNG")
    print(f"Wrote iconset to {ICONSET_DIR}")


def build_icns() -> None:
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)], check=True)
    print(f"Built {ICNS_PATH}")


def main() -> None:
    master = render(SIZE)
    master_path = OUT_DIR / "icon_1024.png"
    master.save(master_path, "PNG")
    print(f"Generated {master_path}")

    write_svg()
    write_iconset()
    build_icns()


if __name__ == "__main__":
    main()
