#!/usr/bin/env python3
"""Convert the supplied logo to native app icons (development-only Pillow)."""

from pathlib import Path

from PIL import Image


def main():
    folder = Path(__file__).resolve().parents[1] / "packaging"
    with Image.open(folder / "logo.png") as source:
        logo = source.convert("RGBA")
    logo.resize((256, 256), Image.Resampling.LANCZOS).save(
        folder / "cometapi-connect.ico",
        sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
    )
    logo.resize((1024, 1024), Image.Resampling.LANCZOS).save(
        folder / "cometapi-connect.icns",
    )


if __name__ == "__main__":
    main()
