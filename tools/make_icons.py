#!/usr/bin/env python3
"""Rebuild the raster icons of the UI from the SVG sources in assets/.

    python3 tools/make_icons.py

Sources (edit these, then run the script): assets/icon.svg (app icon), assets/favicon.svg (simplified mark for
16-32 px), assets/logo.svg and assets/logo-dark.svg (mark + wordmark). Written: favicon.ico (16, 32, 48),
icon-192.png, icon-512.png, apple-touch-icon.png (180), logo.png, logo-dark.png and site.webmanifest.

Needs rsvg-convert (apt install librsvg2-bin) or inkscape, and Pillow for the .ico.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / 'assets'


def render(svg, png, width=None, height=None):
    if shutil.which('rsvg-convert'):
        size = ['-w', str(width)] if width else ['-h', str(height)]
        cmd = ['rsvg-convert', *size, str(svg), '-o', str(png)]
    elif shutil.which('inkscape'):
        size = ['-w', str(width)] if width else ['-h', str(height)]
        cmd = ['inkscape', str(svg), *size, '-o', str(png)]
    else:
        sys.exit("Install rsvg-convert (sudo apt install librsvg2-bin) or inkscape.")
    subprocess.run(cmd, check=True)


def main():
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Install Pillow for the .ico: pip install pillow")
    render(ASSETS / 'icon.svg', ASSETS / 'icon-192.png', 192)
    render(ASSETS / 'icon.svg', ASSETS / 'icon-512.png', 512)
    render(ASSETS / 'icon.svg', ASSETS / 'apple-touch-icon.png', 180)
    render(ASSETS / 'logo.svg', ASSETS / 'logo.png', height=128)
    render(ASSETS / 'logo-dark.svg', ASSETS / 'logo-dark.png', height=128)
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for size in (16, 32, 48):
            png = Path(tmp) / f'{size}.png'
            render(ASSETS / ('favicon.svg' if size < 48 else 'icon.svg'), png, size)
            frames.append(Image.open(png).convert('RGBA'))
        frames[-1].save(ASSETS / 'favicon.ico', format='ICO', append_images=frames[:-1],
                        sizes=[(16, 16), (32, 32), (48, 48)])
    manifest = {
        'name': 'M2 → CSP Migration', 'short_name': 'M2 Migration', 'start_url': '/', 'display': 'standalone',
        'background_color': '#121114', 'theme_color': '#3a1846',
        'icons': [{'src': '/assets/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                  {'src': '/assets/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
                  {'src': '/assets/icon.svg', 'sizes': 'any', 'type': 'image/svg+xml'}]}
    (ASSETS / 'site.webmanifest').write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + '\n',
                                             encoding='utf-8')
    print('Written:', ', '.join(sorted(p.name for p in ASSETS.iterdir())))


if __name__ == '__main__':
    main()
