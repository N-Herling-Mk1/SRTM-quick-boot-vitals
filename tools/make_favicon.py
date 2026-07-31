#!/usr/bin/env python3
"""
tools/make_favicon.py -- generate the tab icon.

Design: a luminous ring with rays firing outward, cyan through blue and
violet into magenta, on black. Matches the TRON-light palette the page
already uses (cyan #00e5ff primary, magenta and violet as the cool tail).

Two forms, because they have different jobs:

  favicon.svg   vector, used by every current browser. Crisp at any size,
                about 4 KB, and editable.
  favicon.ico   16/32/48 raster, for the browsers and OS surfaces that
                still insist on it. Rendered at 4x and downsampled so the
                rays survive minification instead of aliasing into mush.

At 16 px almost nothing survives. So the ring is deliberately heavy and
the rays are few and thick -- an accurate miniature of the source image
would read as a grey smudge in a tab strip.
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(ROOT, "static")

# cyan -> azure -> violet -> magenta, cycled around the ring
PALETTE = [
    (0, 229, 255), (0, 183, 255), (64, 140, 255), (124, 108, 250),
    (168, 92, 240), (214, 78, 220), (240, 74, 178), (255, 90, 140),
    (240, 74, 178), (214, 78, 220), (168, 92, 240), (124, 108, 250),
    (64, 140, 255), (0, 183, 255),
]


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def ray_colour(angle_deg):
    """Colour at a given angle, smoothly cycled through PALETTE."""
    pos = (angle_deg % 360) / 360.0 * len(PALETTE)
    i = int(pos) % len(PALETTE)
    return lerp(PALETTE[i], PALETTE[(i + 1) % len(PALETTE)], pos - int(pos))


# --------------------------------------------------------------------- SVG
def build_svg(size=64, n_rays=72):
    cx = cy = size / 2
    r_in = size * 0.235          # ring radius
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}" role="img">',
        '<title>SRTM quickboot vitals</title>',
        '<defs>',
        '  <radialGradient id="core" cx="50%" cy="50%" r="50%">',
        '    <stop offset="0%" stop-color="#0b1a24"/>',
        '    <stop offset="70%" stop-color="#04070b"/>',
        '    <stop offset="100%" stop-color="#000000"/>',
        '  </radialGradient>',
        '  <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">',
        '    <feGaussianBlur stdDeviation="1.1" result="b"/>',
        '    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>',
        '  </filter>',
        '</defs>',
        f'<rect width="{size}" height="{size}" rx="{size*0.18:.1f}" fill="url(#core)"/>',
        '<g filter="url(#glow)">',
    ]

    for k in range(n_rays):
        a = 360.0 * k / n_rays
        rad = math.radians(a)
        # alternate long and short rays for the spiky texture of the source
        long_ray = (k % 3 == 0)
        r_out = r_in + size * (0.20 if long_ray else 0.125)
        w = size * (0.022 if long_ray else 0.014)
        col = ray_colour(a)
        op = 0.95 if long_ray else 0.55
        x1 = cx + math.cos(rad) * r_in * 0.92
        y1 = cy + math.sin(rad) * r_in * 0.92
        x2 = cx + math.cos(rad) * r_out
        y2 = cy + math.sin(rad) * r_out
        parts.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
            f'stroke="rgb{col}" stroke-width="{w:.2f}" stroke-linecap="round" '
            f'opacity="{op}"/>')

    # the ring itself, drawn as coloured arcs so it picks up the same cycle
    seg = 24
    for k in range(seg):
        a0 = 360.0 * k / seg
        a1 = 360.0 * (k + 1) / seg + 0.8
        p0 = (cx + math.cos(math.radians(a0)) * r_in,
              cy + math.sin(math.radians(a0)) * r_in)
        p1 = (cx + math.cos(math.radians(a1)) * r_in,
              cy + math.sin(math.radians(a1)) * r_in)
        col = ray_colour(a0)
        parts.append(
            f'<path d="M {p0[0]:.2f} {p0[1]:.2f} A {r_in:.2f} {r_in:.2f} 0 0 1 '
            f'{p1[0]:.2f} {p1[1]:.2f}" fill="none" stroke="rgb{col}" '
            f'stroke-width="{size*0.055:.2f}" stroke-linecap="round"/>')

    parts.append('</g>')
    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="{r_in*0.62:.2f}" fill="#04070b" '
        f'opacity="0.92"/>')
    parts.append('</svg>')
    return "\n".join(parts)


# --------------------------------------------------------------------- ICO
def build_raster(size, n_rays=72, ss=4):
    """Render at ss x resolution then downsample, so thin rays survive."""
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    rad_corner = S * 0.18
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=rad_corner,
                        fill=(4, 7, 11, 255))

    cx = cy = S / 2
    r_in = S * 0.235

    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)

    for k in range(n_rays):
        a = 360.0 * k / n_rays
        rad = math.radians(a)
        long_ray = (k % 3 == 0)
        r_out = r_in + S * (0.20 if long_ray else 0.125)
        w = max(1, int(S * (0.022 if long_ray else 0.014)))
        col = ray_colour(a) + (240 if long_ray else 150,)
        gd.line([cx + math.cos(rad) * r_in * 0.92,
                 cy + math.sin(rad) * r_in * 0.92,
                 cx + math.cos(rad) * r_out,
                 cy + math.sin(rad) * r_out], fill=col, width=w)

    seg = 48
    ring_w = max(1, int(S * 0.055))
    for k in range(seg):
        a0, a1 = 360.0 * k / seg, 360.0 * (k + 1) / seg + 1.2
        gd.arc([cx - r_in, cy - r_in, cx + r_in, cy + r_in],
               a0, a1, fill=ray_colour(a0) + (255,), width=ring_w)

    blurred = glow.filter(ImageFilter.GaussianBlur(S * 0.018))
    img = Image.alpha_composite(img, blurred)
    img = Image.alpha_composite(img, glow)

    d2 = ImageDraw.Draw(img)
    rc = r_in * 0.62
    d2.ellipse([cx - rc, cy - rc, cx + rc, cy + rc], fill=(4, 7, 11, 235))

    # re-apply the rounded mask after compositing
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1],
                                           radius=rad_corner, fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(STATIC, exist_ok=True)

    svg_path = os.path.join(STATIC, "favicon.svg")
    with open(svg_path, "w") as f:
        f.write(build_svg())
    print(f"  wrote {svg_path}  ({os.path.getsize(svg_path)} B)")

    for px in (32, 180):
        p = os.path.join(STATIC, f"favicon-{px}.png")
        build_raster(px).save(p)
        print(f"  wrote {p}  ({os.path.getsize(p)} B)")

    ico = os.path.join(STATIC, "favicon.ico")
    build_raster(64).save(ico, sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"  wrote {ico}  ({os.path.getsize(ico)} B)")


if __name__ == "__main__":
    main()
