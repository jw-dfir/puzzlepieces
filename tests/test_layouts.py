import struct

import numpy as np
from PIL import Image

from exporter import export_png
from helpers import puzzle
from layouts import contact_sheet, native_bitmaps
from parser import BIN_MAGIC, ingest


def test_contact_sheet_contains_every_tile_with_no_gaps():
    _, tiles = puzzle(2, 3)
    assembly = contact_sheet(tiles)
    assert len(assembly.placements) == len(tiles)
    assert assembly.settings["layout_kind"] == "contact_sheet"
    columns = assembly.settings["columns"]
    for index, tile in enumerate(tiles):
        p = assembly.placements[tile.id]
        assert (p.x, p.y) == (index % columns * 32, index // columns * 32)


def test_native_duplicates_and_backgrounds_restore_complete_bitmap(tmp_path):
    rgb = np.zeros((64, 64, 3), np.uint8)
    rgb[:32, :32] = [20, 40, 60]
    rgba = np.concatenate((rgb[:, :, ::-1], np.full((64, 64, 1), 255, np.uint8)), axis=2)
    source = tmp_path / "Cache0001.bin"
    source.write_bytes(BIN_MAGIC + struct.pack("<I", 6) + struct.pack("<IIHH", 1, 2, 64, 64) + rgba.tobytes())
    data = ingest([source])
    assert len(data.tiles) == 2 and data.suppressed_count == 1
    tiles, assembly = native_bitmaps(data)
    assert len(tiles) == 4 and len(assembly.placements) == 4
    target = tmp_path / "native.png"
    output = export_png(target, tiles, assembly.placements, layout_kind="native_bitmaps")
    with Image.open(target) as image:
        assert image.size == (64, 64)
        assert np.array_equal(np.asarray(image)[:, :, :3], rgb)
        assert np.all(np.asarray(image)[:, :, 3] == 255)
    assert len(output["tiles"]) == 4 and output["layout_kind"] == "native_bitmaps"


def test_streamed_png_is_pixel_exact_with_crop_scale_and_gaps(tmp_path, monkeypatch):
    import exporter
    from models import Placement

    monkeypatch.setattr(exporter, "MAX_EXPORT_PIXELS", 1)
    expected, tiles = puzzle(1, 2)
    placements = {tile.id: Placement(tile.id, i * 64 - 32, -32, None, None) for i, tile in enumerate(tiles)}
    output = export_png(tmp_path / "stream.png", tiles, placements, scale=2, crop=(-28, -28, 56, -8))
    assert output["streaming"]
    with Image.open(output["path"]) as image:
        pixels = np.asarray(image)[::2, ::2]
        assert np.array_equal(pixels[:, :28, :3], expected[4:24, 4:32])
        assert np.all(pixels[:, 28:60, 3] == 0)
        assert np.array_equal(pixels[:, 60:, :3], expected[4:24, 32:56])
