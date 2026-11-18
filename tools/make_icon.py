"""Generate src/ai_usage_board/resources/app.ico from code (no image library needed).

The mark is three bars of increasing-then-decreasing height - a tiny bar chart
that reads as "levels" at 16px. Run this after changing the brand colours:

    python tools/make_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "ai_usage_board" / "resources" / "app.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)

ACCENT = (37, 99, 235, 255)    # blue
MID = (123, 134, 150, 255)     # slate
FAINT = (154, 163, 176, 255)   # light slate


def bars(size: int):
    """Return (x0, y0, x1, y1, rgba) rectangles in pixel space."""
    unit = size / 16.0
    gap = max(1, round(unit))
    width = max(2, round(unit * 3))
    left = round((size - (width * 3 + gap * 2)) / 2)
    heights = (0.56, 0.86, 0.38)
    colors = (ACCENT, MID, FAINT)
    bottom = size - max(1, round(unit * 2))
    out = []
    for index, (ratio, color) in enumerate(zip(heights, colors)):
        x0 = left + index * (width + gap)
        height = round((bottom - max(1, round(unit * 2))) * ratio) + round(unit * 2)
        out.append((x0, bottom - height, x0 + width, bottom, color))
    return out


def render(size: int) -> bytes:
    pixels = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for x0, y0, x1, y1, color in bars(size):
        radius = 1 if size >= 32 else 0
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                # knock the corners off larger renders so the bars look soft
                if radius:
                    near_x = x < x0 + radius or x >= x1 - radius
                    near_y = y < y0 + radius or y >= y1 - radius
                    if near_x and near_y:
                        continue
                pixels[y][x] = color

    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def build() -> None:
    images = [(size, render(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(header + bytes(entries) + bytes(payload))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
