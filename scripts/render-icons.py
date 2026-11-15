"""Render the Android home-screen icons the manifest names.

Run once when the icon changes, and commit the PNGs it writes:

    uvx --with cairosvg python -I scripts/render-icons.py \
        src/gymlog/icons '#101014' '#7dd3a0' 'H'

PNG rather than only the SVG because Android Chrome mints its installed app
(a WebAPK) from a raster icon, and an SVG-only manifest installs inconsistently.
The maskable icon is full-bleed with the glyph shrunk into the central 80%
safe zone, since the launcher crops it to whatever shape it uses.
"""

import pathlib
import sys

import cairosvg

out, bg, fg, glyph = sys.argv[1:5]
size = int(sys.argv[5]) if len(sys.argv) > 5 else 300


def svg(rx: int, font_size: int) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">'
        f'<rect width="512" height="512" rx="{rx}" fill="{bg}"/>'
        '<text x="256" y="256" text-anchor="middle" dominant-baseline="central"'
        f' fill="{fg}" font-size="{font_size}" font-weight="bold" font-family="DejaVu Sans">'
        f"{glyph}</text></svg>"
    ).encode()


target = pathlib.Path(out)
target.mkdir(parents=True, exist_ok=True)
plain = svg(112, size)
maskable = svg(0, int(size * 0.72))
for name, source, px in (
    ("icon-192.png", plain, 192),
    ("icon-512.png", plain, 512),
    ("icon-maskable-512.png", maskable, 512),
):
    cairosvg.svg2png(
        bytestring=source, write_to=str(target / name), output_width=px, output_height=px
    )
