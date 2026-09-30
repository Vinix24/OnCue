import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
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

# OnCue brand orange (dashboard/css/dashboard.css --secondary / --secondary-light)
GRADIENT_TOP = (251, 146, 60)  # #fb923c — lighter, catches the light from above
GRADIENT_BOTTOM = (249, 115, 22)  # #f97316 — base brand orange, gives depth
BAR_COLOR = (255, 255, 255, 255)  # white — highest contrast on orange at small sizes
STATUS_DOT_COLOR = (34, 211, 238, 255)  # #22d3ee — dashboard's "self/live" cyan accent

# Waveform glyph: symmetric low-mid-high-mid-low bar silhouette
BAR_HEIGHT_FRACTIONS = (0.35, 0.65, 1.0, 0.65, 0.35)
BAR_WIDTH = int(SIZE * 0.088)
BAR_GAP = int(SIZE * 0.052)
BAR_MAX_HEIGHT = int(SIZE * 0.42)
BAR_RADIUS = BAR_WIDTH // 2

STATUS_DOT_RADIUS = int(SIZE * 0.045)
STATUS_DOT_MARGIN = int(SIZE * 0.12)

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


def make_background():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        r = round(GRADIENT_TOP[0] * (1 - t) + GRADIENT_BOTTOM[0] * t)
        g = round(GRADIENT_TOP[1] * (1 - t) + GRADIENT_BOTTOM[1] * t)
        b = round(GRADIENT_TOP[2] * (1 - t) + GRADIENT_BOTTOM[2] * t)
        draw.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))
    return img


def apply_squircle_mask(img):
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (SIZE, SIZE)], radius=RADIUS, fill=255)
    img.putalpha(mask)
    return img


def draw_waveform(img):
    draw = ImageDraw.Draw(img)
    n = len(BAR_HEIGHT_FRACTIONS)
    total_width = n * BAR_WIDTH + (n - 1) * BAR_GAP
    start_x = (SIZE - total_width) // 2
    center_y = SIZE // 2

    for i, frac in enumerate(BAR_HEIGHT_FRACTIONS):
        height = round(BAR_MAX_HEIGHT * frac)
        x0 = start_x + i * (BAR_WIDTH + BAR_GAP)
        x1 = x0 + BAR_WIDTH
        y0 = center_y - height // 2
        y1 = center_y + height // 2
        draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=BAR_RADIUS, fill=BAR_COLOR)
    return img


def draw_status_dot(img):
    draw = ImageDraw.Draw(img)
    cx = SIZE - STATUS_DOT_MARGIN - STATUS_DOT_RADIUS
    cy = SIZE - STATUS_DOT_MARGIN - STATUS_DOT_RADIUS
    draw.ellipse(
        [(cx - STATUS_DOT_RADIUS, cy - STATUS_DOT_RADIUS), (cx + STATUS_DOT_RADIUS, cy + STATUS_DOT_RADIUS)],
        fill=STATUS_DOT_COLOR,
    )
    return img


def write_svg():
    """Emit the mark as SVG for the dashboard header.

    Same geometry as the raster path, so the Dock icon and the in-app mark can
    never drift apart. Vector keeps it sharp at the 34px the headers render it at.
    """
    n = len(BAR_HEIGHT_FRACTIONS)
    total_width = n * BAR_WIDTH + (n - 1) * BAR_GAP
    start_x = (SIZE - total_width) // 2
    center_y = SIZE // 2

    bars = []
    for i, frac in enumerate(BAR_HEIGHT_FRACTIONS):
        height = round(BAR_MAX_HEIGHT * frac)
        x0 = start_x + i * (BAR_WIDTH + BAR_GAP)
        y0 = center_y - height // 2
        bars.append(
            f'  <rect x="{x0}" y="{y0}" width="{BAR_WIDTH}" height="{height}" '
            f'rx="{BAR_RADIUS}" fill="rgb{BAR_COLOR[:3]}" />'
        )

    dot = SIZE - STATUS_DOT_MARGIN - STATUS_DOT_RADIUS
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="OnCue">
  <defs>
    <linearGradient id="oncue-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="rgb{GRADIENT_TOP}" />
      <stop offset="1" stop-color="rgb{GRADIENT_BOTTOM}" />
    </linearGradient>
  </defs>
  <rect width="{SIZE}" height="{SIZE}" rx="{RADIUS}" fill="url(#oncue-bg)" />
{chr(10).join(bars)}
  <circle cx="{dot}" cy="{dot}" r="{STATUS_DOT_RADIUS}" fill="rgb{STATUS_DOT_COLOR[:3]}" />
</svg>
"""
    for path in SVG_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        print(f"Wrote {path}")


def write_iconset(master):
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir()
    for name, size in ICONSET_SIZES:
        resized = master.resize((size, size), Image.LANCZOS)
        resized.save(ICONSET_DIR / name, "PNG")
    print(f"Wrote iconset to {ICONSET_DIR}")


def build_icns():
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)], check=True)
    print(f"Built {ICNS_PATH}")


def main():
    img = make_background()
    img = apply_squircle_mask(img)
    img = draw_waveform(img)
    img = draw_status_dot(img)

    master_path = OUT_DIR / "icon_1024.png"
    img.save(master_path, "PNG")
    print(f"Generated {master_path}")

    write_svg()
    write_iconset(img)
    build_icns()


if __name__ == "__main__":
    main()
