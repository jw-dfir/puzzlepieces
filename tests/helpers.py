import hashlib

import numpy as np

from models import Occurrence, Tile


def tile(rgb, seq=0, group=None):
    rgb = np.ascontiguousarray(rgb)
    digest = hashlib.md5(rgb.tobytes()).hexdigest()
    o = Occurrence("/evidence/tile.bmp", "a" * 64, "b" * 32, seq, 0, group=group)
    return Tile(digest, rgb, digest, [o])


def puzzle(rows=3, cols=4, seed=44):
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, (rows * 32, cols * 32, 3), dtype=np.uint8)
    tiles = [
        tile(pixels[y * 32 : (y + 1) * 32, x * 32 : (x + 1) * 32], y * cols + x)
        for y in range(rows)
        for x in range(cols)
    ]
    return pixels, tiles
