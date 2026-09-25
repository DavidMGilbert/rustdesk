#!/usr/bin/env python3
"""Generate the YLTS icon set (.ico/.png) used by the agent, installers and RustDesk branding.

Replace this with your real logo artwork when you have it: drop a square PNG at
branding/logo-square.png (at least 512x512) and it will be used instead of the drawn mark.
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BRAND = (11, 92, 173, 255)
ACCENT = (127, 209, 255, 255)
GREEN = (46, 184, 92, 255)
AMBER = (240, 160, 30, 255)
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def base_mark(px: int = 1024) -> Image.Image:
    custom = HERE / "logo-square.png"
    if custom.exists():
        return Image.open(custom).convert("RGBA").resize((px, px), Image.LANCZOS)
    s = px / 64
    im = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=int(14 * s), fill=BRAND)
    w = int(4 * s)
    d.rounded_rectangle([10 * s, 17 * s, 54 * s, 44 * s], radius=int(4 * s), outline="white", width=w)
    d.line([24 * s, 53 * s, 40 * s, 53 * s], fill="white", width=w)
    d.line([32 * s, 44 * s, 32 * s, 53 * s], fill="white", width=w)
    # helping hand / download chevron into the screen
    d.line([25 * s, 26 * s, 32 * s, 33 * s, 39 * s, 26 * s], fill=ACCENT, width=w, joint="curve")
    d.line([32 * s, 33 * s, 32 * s, 38 * s], fill=ACCENT, width=w)
    return im


def with_badge(im: Image.Image, colour) -> Image.Image:
    im = im.copy()
    px = im.width
    d = ImageDraw.Draw(im)
    r = px * 0.22
    cx = cy = px - r - px * 0.01
    d.ellipse([cx - r - px * 0.04, cy - r - px * 0.04, cx + r + px * 0.04, cy + r + px * 0.04], fill="white")
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    return im


def save_ico(im: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="ICO", sizes=[(s, s) for s in SIZES])


def main() -> None:
    mark = base_mark()
    out = HERE / "generated"
    save_ico(mark, out / "ylts.ico")
    save_ico(with_badge(mark, GREEN), out / "ylts-active.ico")
    save_ico(with_badge(mark, AMBER), out / "ylts-warn.ico")
    for size in (32, 64, 128, 256, 512):
        mark.resize((size, size), Image.LANCZOS).save(out / f"icon-{size}.png")
    # NSIS installer header/sidebar need BMPs
    side = Image.new("RGB", (164, 314), BRAND[:3])
    side.paste(mark.resize((120, 120), Image.LANCZOS), (22, 40), mark.resize((120, 120), Image.LANCZOS))
    side.save(out / "installer-side.bmp")
    agent_icons = ROOT / "agent" / "icons"
    agent_icons.mkdir(exist_ok=True)
    for name in ("ylts.ico", "ylts-active.ico", "ylts-warn.ico"):
        (agent_icons / name).write_bytes((out / name).read_bytes())
    print("icons written to", out)


if __name__ == "__main__":
    main()
