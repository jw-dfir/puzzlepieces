"""Realistic synthetic Windows screens and RDP8 bitmap caches with known ground truth.

Used by the tests and by ``evaluate.py`` to measure reconstruction accuracy. Screens contain a wallpaper, taskbar,
an Explorer-style window and a console with dense monospaced text. A second screen state scrolls the console and moves
the window, producing visually similar fragments from incompatible states.

The cache writer mimics the behaviour observed in real ``Cache*.bin`` files: each screen update is tiled into 64×64
bitmaps, a tile whose pixels are already cached is not stored again (a cache hit), and records are written
newest-first, so record k-1 is often the right-hand neighbour of record k.
"""

from __future__ import annotations

import hashlib
import random
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

TILE = 64
WORDS = (
    "Payroll Invoice Statement Summary Excellent Power Ltd Month Ending Payslip Accounts Report Legal Reminder "
    "Desktop Documents OneDrive Personal Business Metro Bank Credit Card Assessment Highlights Consolidation Records"
).split()


def _font(names: tuple[str, ...], size: int):
    for name in names:
        for folder in (
            "C:/Windows/Fonts",
            "/usr/share/fonts/truetype/dejavu",
            "/Library/Fonts",
            "/System/Library/Fonts",
        ):
            path = Path(folder) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def mono(size: int = 14):
    return _font(("consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc"), size)


def sans(size: int = 13):
    return _font(("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"), size)


def _path(rng: random.Random) -> str:
    parts = [
        rng.choice(WORDS) + (f" {rng.randint(2019, 2025)}" if rng.random() < 0.4 else "")
        for _ in range(rng.randint(2, 4))
    ]
    return (
        "C:\\Users\\j.doe\\OneDrive - Example LLP\\" + "\\".join(parts) + rng.choice((".pdf", ".xlsx", ".docx", ".jpg"))
    )


def console_lines(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    lines = []
    for _ in range(count):
        if rng.random() < 0.5:
            lines.append(f"100%        New File {rng.randint(1000, 999999):>14}        {_path(rng)}")
        else:
            lines.append(
                f"          {rng.choice(WORDS)} {rng.choice(WORDS)} - {rng.choice(WORDS)} {rng.randint(1, 31)}.pdf"
            )
    return lines


def wallpaper(width: int, height: int) -> Image.Image:
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    r = 0 + 10 * x / width
    g = 90 + 80 * (1 - y / height) + 25 * np.sin(x / 90)
    b = 200 + 40 * (x / width)
    return Image.fromarray(np.clip(np.stack([r, g, b], axis=2), 0, 255).astype(np.uint8))


def draw_titlebar(draw: ImageDraw.ImageDraw, box, title: str, dark=False):
    x0, y0, x1, _ = box
    draw.rectangle((x0, y0, x1, y0 + 30), fill=(255, 255, 255) if not dark else (32, 32, 32))
    draw.text((x0 + 34, y0 + 7), title, font=sans(13), fill=(0, 0, 0) if not dark else (255, 255, 255))
    draw.rectangle((x0 + 10, y0 + 9, x0 + 22, y0 + 21), fill=(240, 200, 60))
    for k, glyph in enumerate(("—", "□", "×")):
        draw.text((x1 - 130 + k * 46, y0 + 6), glyph, font=sans(13), fill=(0, 0, 0) if not dark else (255, 255, 255))


def draw_console(image: Image.Image, box, lines: list[str], first: int):
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    draw_titlebar(draw, box, 'Administrator: Command Prompt - robocopy "C:\\Users\\j.doe"')
    draw.rectangle((x0, y0 + 31, x1, y1), fill=(12, 12, 12))
    font = mono(14)
    for row, y in enumerate(range(y0 + 36, y1 - 16, 16)):
        if first + row < len(lines):
            draw.text((x0 + 6, y), lines[first + row], font=font, fill=(204, 204, 204))
    draw.rectangle((x1 - 16, y0 + 31, x1, y1), fill=(23, 23, 23))
    draw.rectangle((x1 - 13, y0 + 40 + first, x1 - 3, y0 + 120 + first), fill=(77, 77, 77))


def draw_explorer(image: Image.Image, box, seed: int, selected: int):
    rng = random.Random(seed)
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    draw_titlebar(draw, box, "OneDrive - Example LLP")
    draw.rectangle((x0, y0 + 31, x1, y0 + 56), fill=(245, 246, 247))
    for k, tab in enumerate(("File", "Home", "Share", "View")):
        if k == 0:
            draw.rectangle((x0 + 4, y0 + 33, x0 + 52, y0 + 55), fill=(25, 110, 190))
        draw.text((x0 + 14 + k * 60, y0 + 36), tab, font=sans(12), fill=(255, 255, 255) if k == 0 else (30, 30, 30))
    draw.rectangle((x0, y0 + 57, x1, y1), fill=(255, 255, 255))
    draw.rectangle((x0, y0 + 57, x0 + 170, y1), fill=(250, 250, 250))
    for k, label in enumerate(("Quick access", "Desktop", "Downloads", "Documents", "OneDrive", "This PC", "Network")):
        draw.rectangle((x0 + 12, y0 + 70 + k * 24, x0 + 26, y0 + 84 + k * 24), fill=(80, 150, 230))
        draw.text((x0 + 34, y0 + 68 + k * 24), label, font=sans(12), fill=(20, 20, 20))
    for column, (title, offset) in enumerate((("Name", 190), ("Date modified", 470), ("Type", 640), ("Size", 760))):
        if x0 + offset < x1 - 40:
            draw.text((x0 + offset, y0 + 62), title, font=sans(12), fill=(90, 90, 90))
    for row in range((y1 - y0 - 90) // 22):
        y = y0 + 84 + row * 22
        if row == selected:
            draw.rectangle((x0 + 180, y - 1, x1 - 8, y + 20), fill=(204, 232, 255))
        name = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {rng.randint(1, 99)}.pdf"
        draw.rectangle(
            (x0 + 190, y + 3, x0 + 202, y + 17), fill=(220, 60, 50) if rng.random() < 0.5 else (40, 120, 200)
        )
        draw.text((x0 + 210, y + 2), name, font=sans(12), fill=(20, 20, 20))
        draw.text(
            (x0 + 470, y + 2),
            f"{rng.randint(1, 28):02d}/0{rng.randint(1, 9)}/2024 1{rng.randint(0, 9)}:{rng.randint(10, 59)}",
            font=sans(12),
            fill=(90, 90, 90),
        )
        draw.text((x0 + 640, y + 2), "PDF File", font=sans(12), fill=(90, 90, 90))
        if x0 + 760 < x1 - 40:
            draw.text((x0 + 760, y + 2), f"{rng.randint(12, 4800):,} KB", font=sans(12), fill=(90, 90, 90))


def draw_taskbar(image: Image.Image):
    draw = ImageDraw.Draw(image)
    width, height = image.size
    draw.rectangle((0, height - 40, width, height), fill=(20, 32, 48))
    draw.rectangle((50, height - 34, 330, height - 6), fill=(242, 242, 242))
    draw.text((84, height - 30), "Type here to search", font=sans(13), fill=(60, 60, 60))
    for k, colour in enumerate(((0, 120, 215), (80, 90, 200), (240, 180, 40), (40, 160, 90), (200, 60, 60))):
        draw.rectangle((360 + k * 48, height - 32, 384 + k * 48, height - 8), fill=colour)
    draw.text((width - 110, height - 36), "14:07", font=sans(12), fill=(255, 255, 255))
    draw.text((width - 120, height - 20), "02/07/2026", font=sans(12), fill=(255, 255, 255))


@dataclass
class Scenario:
    states: list[np.ndarray]
    # (state index, x0, y0, x1, y1) dirty rectangles in tile units, in update order
    updates: list[tuple[int, int, int, int, int]] = field(default_factory=list)


def scenario(width_tiles: int = 20, height_tiles: int = 12, seed: int = 7) -> Scenario:
    width, height = width_tiles * TILE, height_tiles * TILE
    lines = console_lines(seed, 120)
    states = []
    explorer_width = min(700, int(width * 0.55))
    explorer_top = int(height * 0.39)
    for state, (first, explorer_x, selected) in enumerate(((0, 40, 3), (3, 104, 5))):
        image = wallpaper(width, height)
        draw_explorer(image, (explorer_x, explorer_top, explorer_x + explorer_width, height - 70), seed + 1, selected)
        draw_console(image, (int(width * 0.38), 20, width - 30, int(height * 0.62)), lines, first)
        draw_taskbar(image)
        states.append(np.asarray(image.convert("RGB")))
    updates = [(0, 0, 0, width_tiles, height_tiles)]
    # Second state: only the changed area is redrawn (console scroll + window move).
    changed = np.zeros((height_tiles, width_tiles), bool)
    for ty in range(height_tiles):
        for tx in range(width_tiles):
            a = states[0][ty * TILE : (ty + 1) * TILE, tx * TILE : (tx + 1) * TILE]
            b = states[1][ty * TILE : (ty + 1) * TILE, tx * TILE : (tx + 1) * TILE]
            changed[ty, tx] = not np.array_equal(a, b)
    ys, xs = np.nonzero(changed)
    if len(xs):
        updates.append((1, int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return Scenario(states, updates)


def write_cache(path: str | Path, sc: Scenario, seed: int = 0, drop: float = 0.0) -> list[dict]:
    """Write an RDP8bmp v6 cache. Returns per-record ground truth in file order.

    ``drop`` randomly evicts that fraction of records, as happens when a cache is full.
    """
    rng = random.Random(seed)
    stored: dict[bytes, int] = {}
    order: list[tuple[np.ndarray, dict]] = []
    for state, x0, y0, x1, y1 in sc.updates:
        screen = sc.states[state]
        for ty in range(y0, y1):
            for tx in range(x0, x1):
                tile = np.ascontiguousarray(screen[ty * TILE : (ty + 1) * TILE, tx * TILE : (tx + 1) * TILE])
                digest = hashlib.sha256(tile.tobytes()).digest()
                if digest in stored:
                    continue  # cache hit: not stored again
                stored[digest] = len(order)
                order.append((tile, {"state": state, "x": tx, "y": ty, "sha256": digest.hex()}))
    order = [item for item in order if rng.random() >= drop]
    order.reverse()  # newest first
    with open(path, "wb") as stream:
        stream.write(b"RDP8bmp\x00" + struct.pack("<I", 6))
        for index, (tile, _) in enumerate(order):
            bgra = np.concatenate([tile[:, :, ::-1], np.full((TILE, TILE, 1), 255, np.uint8)], axis=2)
            stream.write(struct.pack("<IIHH", index, 0x5EED, TILE, TILE) + bgra.tobytes())
    return [truth for _, truth in order]


def screen_index(sc: Scenario, tile: int = TILE) -> list[dict[bytes, list[tuple[int, int]]]]:
    """Per state, map tile pixel SHA-256 to all grid positions where those pixels appear."""
    result = []
    for screen in sc.states:
        mapping: dict[bytes, list[tuple[int, int]]] = {}
        for ty in range(screen.shape[0] // tile):
            for tx in range(screen.shape[1] // tile):
                data = np.ascontiguousarray(screen[ty * tile : (ty + 1) * tile, tx * tile : (tx + 1) * tile]).tobytes()
                mapping.setdefault(hashlib.sha256(data).digest(), []).append((tx, ty))
        result.append(mapping)
    return result
