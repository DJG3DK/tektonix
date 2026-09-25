"""Generate every Tektonix brand asset from one geometry definition.

Mark: 09 PLUMB -- a plumb line. The bar and cord in bone, the bob in brass.
A plumb line is the oldest instrument for deciding whether a thing is true,
which is the job, and it contains a T for free.

Palette: DRAFTING. Brass #C9A227 on blue-graphite. Every foreground here was
measured against --surface #1B2028 and clears 4.5:1 (see theme.css).

Everything is drawn at SS x and downsampled with LANCZOS -- ImageDraw is
hard-aliased, and a logo with jagged diagonals is worse than no logo.
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
SS = 4

BG = (0x11, 0x15, 0x1A, 255)
SURFACE = (0x1B, 0x20, 0x28, 255)
BORDER = (0x34, 0x3C, 0x48, 255)
TEXT = (0xED, 0xF0, 0xF4, 255)
DIM = (0xA3, 0xAD, 0xBB, 255)
BRASS = (0xC9, 0xA2, 0x27, 255)
CLEAR = (0, 0, 0, 0)

BLACK = "/usr/share/fonts/truetype/lato/Lato-Black.ttf"
BOLD = "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
REG = "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# ── the mark, in a 64x64 unit box ───────────────────────────────────────────
BAR = [(18, 8), (46, 8), (46, 16), (18, 16)]      # the fixing point
CORD = [(29.5, 16), (34.5, 16), (34.5, 38), (29.5, 38)]
BOB = [(24, 38), (40, 38), (32, 58)]               # the only part that moves


def plumb(d, ox, oy, size, stem=TEXT, bob=BRASS):
    k = size / 64.0
    for pts, col in ((BAR, stem), (CORD, stem), (BOB, bob)):
        d.polygon([(ox + x * k, oy + y * k) for x, y in pts], fill=col)


def _new(w, h, bg=CLEAR):
    img = Image.new("RGBA", (w * SS, h * SS), bg)
    return img, ImageDraw.Draw(img)


def _save(img, w, h, path, rgb=False):
    out = img.resize((w, h), Image.LANCZOS)
    if rgb:
        out = out.convert("RGB")
    out.save(path)
    print(f"  {path.name:<26} {w}x{h}")


# ── 1. the lockup, for the sidebar and the landing page ─────────────────────
def lockup(path, pad_ratio=0.16):
    """Replaces 3d-agent-logo.png. Composed on an oversized canvas, then
    cropped to the actual ink and padded evenly -- hand-placed coordinates
    gave 68px of left margin against 16px of right, which reads as a mistake
    at any size. The CSS is `height: 30px; width: auto`, so the exact pixel
    ratio does not matter; the balance does."""
    W, H = 1400, 400
    img, d = _new(W, H)
    mark = 210
    plumb(d, 40 * SS, ((H - mark) // 2) * SS, mark * SS)
    f = ImageFont.truetype(BLACK, 150 * SS)
    box = d.textbbox((0, 0), "Tektonix", font=f)
    d.text((272 * SS, (H * SS - (box[3] - box[1])) / 2 - box[1]),
           "Tektonix", font=f, fill=TEXT)

    ink = img.getbbox()
    pad = int((ink[3] - ink[1]) * pad_ratio)
    img = img.crop((ink[0] - pad, ink[1] - pad, ink[2] + pad, ink[3] + pad))
    w, h = img.width // SS, img.height // SS
    _save(img, w, h, path)


# ── 2. favicons ─────────────────────────────────────────────────────────────
def icon(path, px, ground=SURFACE, radius=0.18):
    """Solid ground on purpose. The mark's cord and bar are light, so a
    transparent favicon disappears against a light browser tab; a filled tile
    is legible in either tab theme."""
    img, d = _new(px, px, CLEAR)
    r = int(px * radius) * SS
    d.rounded_rectangle([0, 0, px * SS - 1, px * SS - 1], radius=r, fill=ground)
    inset = px * 0.16
    plumb(d, inset * SS, inset * SS, (px - 2 * inset) * SS)
    _save(img, px, px, path)


def ico(path, sizes=(16, 32, 48)):
    """One .ico carrying several real renders -- Windows and some browsers
    pick the nearest, and a single upscaled 16px looks like mud at 48."""
    frames = []
    for px in sizes:
        img, d = _new(px, px, CLEAR)
        r = int(px * 0.18) * SS
        d.rounded_rectangle([0, 0, px * SS - 1, px * SS - 1], radius=r, fill=SURFACE)
        inset = px * 0.16
        plumb(d, inset * SS, inset * SS, (px - 2 * inset) * SS)
        frames.append(img.resize((px, px), Image.LANCZOS))
    frames[0].save(path, format="ICO", sizes=[(f.width, f.height) for f in frames],
                   append_images=frames[1:])
    print(f"  {path.name:<26} {list(sizes)}")


def apple_touch(path, px=180):
    """iOS composites onto white if there is any alpha and applies its own
    corner mask, so this one is full-bleed and fully opaque."""
    img, d = _new(px, px, SURFACE)
    plumb(d, px * 0.2 * SS, px * 0.16 * SS, px * 0.6 * SS)
    _save(img, px, px, path)


def pwa_icon(path, px, maskable=False):
    """The installed-app icon, for the web app manifest.

    Two shapes, because Android draws them differently. `any` is the tile as
    it ships -- rounded, the mark at the same 0.16 inset as the favicons.
    `maskable` is full-bleed and keeps every drawn pixel inside the safe
    zone: Android crops an adaptive icon to whatever mask the launcher uses
    (circle, squircle, teardrop), guaranteeing only the middle 80% survives.
    A rounded tile handed to that crop loses its own corners and reads as a
    dark blob, which is what "why is my app icon a grey circle" always is.
    """
    ground = SURFACE
    img, d = _new(px, px, ground if maskable else CLEAR)
    if not maskable:
        r = int(px * 0.18) * SS
        d.rounded_rectangle([0, 0, px * SS - 1, px * SS - 1], radius=r, fill=ground)
    # 0.16 of the tile normally; 0.26 for maskable, which keeps the mark
    # inside the 80%-diameter circle every launcher mask is guaranteed to
    # leave alone.
    inset = px * (0.26 if maskable else 0.16)
    plumb(d, inset * SS, inset * SS, (px - 2 * inset) * SS)
    _save(img, px, px, path)


# ── 3. the display card ─────────────────────────────────────────────────────
def og_card(path, w=1200, h=630):
    """What a link to tektonix.io unfurls as in Slack, iMessage, X."""
    img, d = _new(w, h, BG)
    s = lambda v: int(v * SS)  # noqa: E731

    # a faint drafting grid -- the subject's own paper
    for x in range(0, w, 40):
        d.line([(s(x), 0), (s(x), s(h))], fill=(0x18, 0x1D, 0x24, 255), width=SS)
    for y in range(0, h, 40):
        d.line([(0, s(y)), (s(w), s(y))], fill=(0x18, 0x1D, 0x24, 255), width=SS)

    # the plumb line, dropped the full height of the card, brass bob resting
    d.rectangle([s(975), 0, s(981), s(300)], fill=(0x2A, 0x31, 0x3C, 255))
    plumb(d, s(930), s(260), s(190))

    plumb(d, s(92), s(86), s(104))
    f_word = ImageFont.truetype(BLACK, s(76))
    d.text((s(214), s(96)), "Tektonix", font=f_word, fill=TEXT)

    f_h = ImageFont.truetype(BOLD, s(44))
    d.text((s(92), s(268)), "An autonomous coding agent", font=f_h, fill=TEXT)
    d.text((s(92), s(322)), "that ships.", font=f_h, fill=BRASS)

    f_b = ImageFont.truetype(REG, s(25))
    for i, line in enumerate([
            "Plans the work, writes the code, runs your real test",
            "suite, and passes an independent review gate before",
            "anything merges."]):
        d.text((s(92), s(398 + i * 36)), line, font=f_b, fill=DIM)

    d.line([(s(92), s(536)), (s(1108), s(536))], fill=BORDER, width=SS)
    f_m = ImageFont.truetype(MONO, s(19))
    d.text((s(92), s(560)), "PLAN  ·  BUILD  ·  VERIFY  ·  REVIEW  ·  SHIP", font=f_m, fill=DIM)
    tw = d.textlength("tektonix.io", font=f_m)
    d.text((s(1108) - tw, s(560)), "tektonix.io", font=f_m, fill=BRASS)

    _save(img, w, h, path, rgb=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    print("building Tektonix assets into", OUT)
    lockup(OUT / "tektonix-logo.png")
    icon(OUT / "favicon-16x16.png", 16)
    icon(OUT / "favicon-32x32.png", 32)
    ico(OUT / "favicon.ico")
    apple_touch(OUT / "apple-touch-icon.png")
    pwa_icon(OUT / "icon-192.png", 192)
    pwa_icon(OUT / "icon-512.png", 512)
    pwa_icon(OUT / "icon-maskable-512.png", 512, maskable=True)
    og_card(OUT / "og-preview.png")
