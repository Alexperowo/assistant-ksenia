from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать прозрачную PNG-эмблему и многоразмерную иконку Windows."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--png", type=Path, required=True)
    parser.add_argument("--ico", type=Path, required=True)
    return parser.parse_args()


def connected_background(image: Image.Image, tolerance: int = 32) -> Image.Image:
    """Select only background pixels connected to the canvas border.

    The generated master has a near-black exterior. Connectivity is important:
    the dark navy face inside the violet rim must remain fully opaque.
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    key = pixels[0, 0]

    def matches(x: int, y: int) -> bool:
        value = pixels[x, y]
        return max(abs(value[index] - key[index]) for index in range(3)) <= tolerance

    selected = bytearray(width * height)
    pending: deque[tuple[int, int]] = deque()
    for x in range(width):
        pending.append((x, 0))
        pending.append((x, height - 1))
    for y in range(height):
        pending.append((0, y))
        pending.append((width - 1, y))

    while pending:
        x, y = pending.popleft()
        offset = y * width + x
        if selected[offset] or not matches(x, y):
            continue
        selected[offset] = 255
        if x:
            pending.append((x - 1, y))
        if x + 1 < width:
            pending.append((x + 1, y))
        if y:
            pending.append((x, y - 1))
        if y + 1 < height:
            pending.append((x, y + 1))

    mask = Image.frombytes("L", (width, height), bytes(selected))
    # Contract the emblem by about one source pixel to remove a dark fringe,
    # then feather only the antialiased outer edge.
    return mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.55))


def main() -> int:
    args = parse_args()
    source = Image.open(args.input).convert("RGBA")
    background = connected_background(source)
    alpha = background.point(lambda value: 255 - value)
    source.putalpha(alpha)

    args.png.parent.mkdir(parents=True, exist_ok=True)
    source.save(args.png, format="PNG", optimize=True)
    source.save(
        args.ico,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
