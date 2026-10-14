import hashlib
import json
import struct
from threading import Event

import numpy as np
import pytest
from PIL import Image

from models import Cancelled
from parser import BIN_MAGIC, CacheFormatError, decode_interleaved, ingest, iter_cache, pixels_to_rgb


def test_bmp_dedupe_filter_and_dimensions(tmp_path):
    rng = np.random.default_rng(5)
    tile = rng.integers(0, 256, (32, 32, 3), dtype=np.uint8)
    for i in range(2):
        Image.fromarray(tile).save(tmp_path / f"tile{i}.bmp")
    for i in range(3):
        Image.new("RGB", (32, 32), (3, 33, 92)).save(tmp_path / f"blank{i}.bmp")
    Image.new("RGB", (17, 9), (12, 11, 8)).save(tmp_path / "partial.bmp")
    data = ingest([tmp_path])
    assert data.raw_count == 6 and data.duplicate_count == 3
    assert len(data.tiles) == 3 and data.suppressed_count == 1
    assert len(data.active()) == 2
    assert not data.warnings
    assert len(next(t for t in data.tiles if t.suppressed).occurrences) == 3
    assert all(len(s["sha256"]) == 64 and len(s["md5"]) == 32 for s in data.sources)


@pytest.mark.parametrize("version", [1, 6])
def test_bin_non64_dimensions_orientation_and_split(tmp_path, version):
    rgb = np.arange(40 * 48 * 3, dtype=np.uint16).astype(np.uint8).reshape(40, 48, 3)
    bgra = np.concatenate((rgb[:, :, ::-1], np.full((40, 48, 1), 255, np.uint8)), axis=2)
    path = tmp_path / "Cache0001.bin"
    path.write_bytes(BIN_MAGIC + struct.pack("<I", version) + struct.pack("<IIHH", 42, 19, 48, 40) + bgra.tobytes())
    parsed = list(iter_cache(path))
    assert np.array_equal(parsed[0][0], rgb)
    assert parsed[0][2] == 12
    data = ingest([path])
    assert len(data.tiles) == 4
    first = data.tiles[0].occurrences[0]
    assert first.native_width == 48 and first.native_height == 40
    assert any(tile.rgb.shape == (8, 16, 3) for tile in data.tiles)


def test_pixel_md5_is_hash_of_decoded_pixels(tmp_path):
    rgb = np.arange(32 * 32 * 3, dtype=np.uint16).astype(np.uint8).reshape(32, 32, 3)
    Image.fromarray(rgb).save(tmp_path / "tile.bmp")
    tile = ingest([tmp_path]).tiles[0]
    assert tile.md5 == hashlib.md5(rgb.tobytes()).hexdigest()
    assert len(tile.id) == 64


@pytest.mark.parametrize("bpp", [1, 2, 3, 4])
def test_bmc_fixed_slots_and_orientation(tmp_path, bpp):
    path = tmp_path / {1: "bcache2.bmc", 2: "bcache22.bmc", 3: "bcache23.bmc", 4: "bcache24.bmc"}[bpp]
    w, h = 64, 32
    pixels = bytes((i * 17) % 256 for i in range(w * h * bpp))
    header = struct.pack("<IIHHII", 10, 11, w, h, len(pixels), 0)
    path.write_bytes(
        header + pixels + bytes(4096 * bpp - len(pixels)) + header + pixels + bytes(4096 * bpp - len(pixels))
    )
    parsed = list(iter_cache(path))
    expected = pixels_to_rgb(pixels, w, h, bpp, True)
    assert len(parsed) == 2 and np.array_equal(parsed[0][0], expected)
    assert parsed[1][2] == 20 + 4096 * bpp
    assert not ingest([path]).warnings


def test_rle_all_order_families():
    literal = b"\xf4\x08\x00" + bytes(range(8))
    assert decode_interleaved(literal, 8, 1, 1) == bytes(range(8))
    assert decode_interleaved(b"\x68\x14", 8, 1, 1) == bytes([20]) * 8
    assert decode_interleaved(b"\xe4\x01\x02", 8, 1, 1) == b"\x01\x02" * 4
    assert decode_interleaved(b"\x28", 8, 1, 1) == b"\xff" * 8
    assert decode_interleaved(b"\xc8\x55", 8, 1, 1) == b"\x55" * 8
    assert decode_interleaved(b"\x41\xaa", 8, 1, 1) == b"\x00\xff" * 4
    assert decode_interleaved(b"\xd1\x55\xaa", 8, 1, 1) == b"\x00\x55" * 4
    assert decode_interleaved(b"\xf9", 8, 1, 1) == b"\xff\xff" + b"\x00" * 6
    assert decode_interleaved(b"\xfa", 8, 1, 1) == b"\xff\x00\xff" + b"\x00" * 5
    assert decode_interleaved(b"\xfd\xfe", 2, 1, 2) == b"\xff\xff\x00\x00"
    assert decode_interleaved(b"\x04\x04", 8, 1, 1) == b"\x00" * 4 + b"\xff" + b"\x00" * 3
    assert decode_interleaved(b"\x08\x08", 8, 2, 1) == b"\x00" * 16
    assert decode_interleaved(b"\x88" + bytes(range(8)) + b"\x28", 8, 2, 1) == bytes(range(8)) + bytes(
        i ^ 255 for i in range(8)
    )
    for stream in (b"\xf4\xff\xff", b"\xf5", b"\xf4\x08\x00", b"\x00", b"\xa0"):
        with pytest.raises(CacheFormatError):
            decode_interleaved(stream, 8, 1, 1)


def test_compressed_bmc_and_rgb565(tmp_path):
    path = tmp_path / "bcache22.bmc"
    stream = b"\xf3" + struct.pack("<H", 4096) + b"\x00\xf8"
    path.write_bytes(struct.pack("<IIHHII", 1, 2, 64, 64, len(stream), 8) + stream + bytes(8192 - len(stream)))
    parsed = list(iter_cache(path))
    assert np.all(parsed[0][0] == [255, 0, 0])


def test_corrupt_input_retains_prior_valid_record(tmp_path):
    path = tmp_path / "Cache0000.bin"
    path.write_bytes(BIN_MAGIC + struct.pack("<I", 1) + struct.pack("<IIHH", 1, 2, 32, 32) + bytes(4096) + b"broken")
    data = ingest([path])
    assert len(data.tiles) == 1
    assert len(data.warnings) == 1 and "Truncated" in data.warnings[0]
    path.write_bytes(BIN_MAGIC + struct.pack("<I", 9))
    assert "Unsupported" in ingest([path]).warnings[0]


def test_metadata_and_cancellation(tmp_path):
    Image.new("RGB", (32, 32), (2, 3, 4)).save(tmp_path / "test12.bmp")
    (tmp_path / "tile_metadata.json").write_text(
        json.dumps({"test12.bmp": {"timestamp": "2026-10-01T12:00:00Z", "group": "A"}})
    )
    occurrence = ingest([tmp_path]).tiles[0].occurrences[0]
    assert occurrence.group == "A" and occurrence.timestamp == 1790856000
    assert occurrence.sequence == 12
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        ingest([tmp_path], cancel=cancelled)
