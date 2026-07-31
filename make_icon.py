"""Generate the Kort og Godt app icon: a sleek HOLOGRAPHIC trading card,
matte black with gold accents.

Produces kort_og_godt.ico (multi-resolution) and kort_og_godt.png (preview).
Run:  .venv\\Scripts\\python make_icon.py
"""
import colorsys
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
S = 1024

# Palette — matte black + gold, holographic art
BLACK = (16, 16, 22)
BLACK_HI = (30, 30, 40)
GOLD = (214, 176, 92)
GOLD_HI = (245, 222, 150)
GOLD_DK = (150, 116, 42)


def font(names, size):
    for n in names:
        for base in (r"C:\Windows\Fonts", "/usr/share/fonts"):
            p = Path(base) / n
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    pass
    return ImageFont.load_default()


SERIF = ["georgiab.ttf", "timesbd.ttf", "DejaVuSerif-Bold.ttf"]
SANS = ["ariblk.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]


def fit_font(names, text, max_w, start):
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    size = start
    while size > 10:
        f = font(names, size)
        bb = probe.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 3
    return font(names, 10)


def star(d, cx, cy, r, color, points=4, inner=0.4):
    pts = []
    for i in range(points * 2):
        ang = math.pi * i / points - math.pi / 2
        rr = r if i % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=color)


def holo(w, h, radius):
    """Holographic panel: iridescent diagonal foil + a glossy sweep."""
    lo = 360
    base = Image.new("RGB", (lo, lo))
    px = base.load()
    for y in range(lo):
        for x in range(lo):
            t = (x + y) / (2 * lo)
            hue = (0.55 + t * 1.15) % 1.0
            wobble = 0.06 * math.sin((x - y) / 26.0)
            r, g, b = colorsys.hsv_to_rgb((hue + wobble) % 1.0, 0.52, 0.92)
            px[x, y] = (int(r * 255), int(g * 255), int(b * 255))
    panel = base.resize((w, h), Image.LANCZOS).convert("RGBA")

    # Glossy diagonal sweep.
    gloss = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(gloss)
    bw = int(w * 0.22)
    for i in range(-h, w, 1):
        pass
    gd.polygon([(w * 0.15, 0), (w * 0.15 + bw, 0),
                (w * 0.45 + bw, h), (w * 0.45, h)], fill=90)
    gloss = gloss.filter(ImageFilter.GaussianBlur(24))
    white = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    white.putalpha(gloss)
    panel = Image.alpha_composite(panel, white)

    # Darken toward edges (vignette) for depth on black.
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).rounded_rectangle(
        [int(w * 0.06), int(h * 0.06), int(w * 0.94), int(h * 0.94)],
        radius=radius, fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(30))
    panel.putalpha(vig)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=radius, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(panel, (0, 0), mask)
    return out


def gem(d, cx, cy, r):
    """A faceted gold diamond with a sparkle."""
    top = (cx, cy - r)
    bot = (cx, cy + r * 1.25)
    left = (cx - r * 0.82, cy - r * 0.15)
    right = (cx + r * 0.82, cy - r * 0.15)
    ml = (cx - r * 0.42, cy - r * 0.15)
    mr = (cx + r * 0.42, cy - r * 0.15)
    d.polygon([left, right, bot], fill=GOLD)
    d.polygon([top, left, right], fill=GOLD_HI)
    d.polygon([left, ml, bot], fill=GOLD_DK)
    d.polygon([right, mr, bot], fill=GOLD_DK)
    d.polygon([ml, mr, bot], fill=GOLD)
    d.line([left, right], fill=GOLD_DK, width=3)
    d.line([ml, bot], fill=GOLD_HI, width=2)
    d.line([mr, bot], fill=GOLD_HI, width=2)
    d.line([top, bot], fill=GOLD_HI, width=2)
    star(d, cx + r * 0.9, cy - r * 0.85, r * 0.28, (255, 255, 255))


def corner_bracket(d, x, y, sx, sy, size, color, w):
    d.line([(x, y), (x + sx * size, y)], fill=color, width=w)
    d.line([(x, y), (x, y + sy * size)], fill=color, width=w)


def render() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Matte black card with a faint top glow.
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.17), fill=BLACK)
    glow = Image.new("L", (S, S), 0)
    gd = ImageDraw.Draw(glow)
    for i in range(int(S * 0.5)):
        gd.line([(0, i), (S, i)], fill=int(26 * (1 - i / (S * 0.5))))
    shape = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, S - 1, S - 1],
                                            radius=int(S * 0.17), fill=255)
    glow = ImageChops.multiply(glow, shape)
    hi = Image.new("RGBA", (S, S), BLACK_HI + (0,))
    hi.putalpha(glow)
    img.alpha_composite(hi)

    # Double gold border (slim, premium).
    d.rounded_rectangle([int(S * 0.035)] * 2 + [int(S * 0.965)] * 2,
                        radius=int(S * 0.14), outline=GOLD, width=7)
    d.rounded_rectangle([int(S * 0.065)] * 2 + [int(S * 0.935)] * 2,
                        radius=int(S * 0.115), outline=GOLD_DK, width=3)

    m = int(S * 0.11)
    # Corner brackets.
    b = int(S * 0.055)
    for (x, y, sx, sy) in [(m, m, 1, 1), (S - m, m, -1, 1),
                           (m, S - m, 1, -1), (S - m, S - m, -1, -1)]:
        corner_bracket(d, x, y, sx, sy, b, GOLD_HI, 6)

    # Title.
    tfont = fit_font(SERIF, "Kort og Godt", int(S * 0.66), int(S * 0.115))
    bb = d.textbbox((0, 0), "Kort og Godt", font=tfont)
    ty = int(S * 0.135)
    d.text(((S - (bb[2] - bb[0])) / 2 - bb[0], ty), "Kort og Godt",
           font=tfont, fill=GOLD_HI)
    ly = ty + (bb[3] - bb[1]) + int(S * 0.03)
    d.line([(S * 0.28, ly), (S * 0.72, ly)], fill=GOLD, width=4)

    # Holographic art window.
    ax0, ax1 = int(S * 0.145), int(S * 0.855)
    ay0, ay1 = int(S * 0.30), int(S * 0.74)
    panel = holo(ax1 - ax0, ay1 - ay0, 26)
    img.alpha_composite(panel, (ax0, ay0))
    d.rounded_rectangle([ax0, ay0, ax1, ay1], radius=26, outline=GOLD, width=6)
    d.rounded_rectangle([ax0 - 4, ay0 - 4, ax1 + 4, ay1 + 4], radius=30,
                        outline=GOLD_DK, width=2)

    # Gold gem emblem on the holo.
    gem(d, S / 2, (ay0 + ay1) / 2, int(S * 0.11))

    # Footer: gold rarity stars.
    fy = int(S * 0.815)
    d.line([(S * 0.30, fy - int(S * 0.02)), (S * 0.70, fy - int(S * 0.02))],
           fill=GOLD_DK, width=3)
    for k in (-1, 0, 1):
        star(d, S / 2 + k * int(S * 0.07), fy + int(S * 0.02),
             int(S * 0.026), GOLD, points=5, inner=0.45)

    return img


def main():
    master = render()
    master.resize((512, 512), Image.LANCZOS).save(HERE / "kort_og_godt.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = [master.resize((s, s), Image.LANCZOS) for s in sizes]
    icons[-1].save(HERE / "kort_og_godt.ico", format="ICO",
                   sizes=[(s, s) for s in sizes],
                   append_images=icons[:-1])
    print("wrote", HERE / "kort_og_godt.ico", "and kort_og_godt.png")


if __name__ == "__main__":
    main()
