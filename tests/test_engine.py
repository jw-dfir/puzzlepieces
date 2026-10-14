import random
import struct
from threading import Event

import numpy as np
import pytest
from PIL import Image, ImageDraw

import synthetic
from engine import (
    FILL_REASON,
    Blocks,
    EdgeSet,
    EngineSettings,
    Record,
    Scorer,
    SeamModel,
    _fill_holes,
    build_records,
    combined,
    stitch,
)
from evaluate import adjacency_metrics, run
from models import Cancelled, Placement, Tile
from parser import BIN_MAGIC, ingest


def write_bin(path, tiles):
    with open(path, "wb") as stream:
        stream.write(BIN_MAGIC + struct.pack("<I", 6))
        for i, rgb in enumerate(tiles):
            h, w = rgb.shape[:2]
            bgra = np.concatenate([rgb[:, :, ::-1], np.full((h, w, 1), 255, np.uint8)], axis=2)
            stream.write(struct.pack("<IIHH", i, 7, w, h) + bgra.tobytes())


@pytest.fixture(scope="module")
def synthetic_result():
    return run(seed=7, width_tiles=14, height_tiles=9)


def test_realistic_screen_reconstruction_quality(synthetic_result):
    # Known-adjacency fixture: wallpaper, taskbar, Explorer, dense console text, a scrolled/moved second state,
    # cache hits for repeated tiles and newest-first record order.
    m = synthetic_result
    assert m["records"] > 100
    assert m["precision"] >= 0.95, m
    assert m["recall"] >= 0.25, m
    assert m["coherent_regions_ge4"] >= 3, m
    assert m["largest_region"] >= 10, m


def test_evicted_cache_still_mostly_correct():
    m = run(seed=11, drop=0.15, width_tiles=14, height_tiles=9)
    assert m["precision"] is None or m["precision"] >= 0.85, m


def test_shuffled_terminal_text_without_order_is_not_joined(tmp_path):
    # Dense console text on a shared black background is the hard case: without cache order, single seams are
    # ambiguous and must not be joined into a collage.
    sc = synthetic.scenario(14, 9, seed=5)
    screen = sc.states[0]
    tiles = []
    for ty in range(1, 5):
        for tx in range(6, 13):
            tiles.append(np.ascontiguousarray(screen[ty * 64 : (ty + 1) * 64, tx * 64 : (tx + 1) * 64]))
    random.Random(3).shuffle(tiles)
    folder = tmp_path / "tiles"
    folder.mkdir()
    for i, rgb in enumerate(tiles):
        Image.fromarray(rgb).save(folder / f"tile{i:04d}.bmp")
    data = ingest([folder])
    result = stitch(data.tiles, EngineSettings(use_sequence=False))
    metrics = adjacency_metrics(result, result.instances, sc)
    assert metrics["false_joins"] <= 1, metrics


def test_records_restore_duplicate_subtiles_and_keep_separate_identity(tmp_path):
    shared = np.zeros((32, 32, 3), np.uint8)
    first = np.random.default_rng(1).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    second = np.random.default_rng(2).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    first[:32, :32] = shared
    second[32:, 32:] = shared
    source = tmp_path / "Cache0000.bin"
    write_bin(source, [first, second, first])
    data = ingest([source])
    assert data.duplicate_count >= 1
    records = build_records(data.tiles)
    assert len(records) == 3  # the repeated record is kept as its own occurrence
    by_sequence = {r.order: r.tile for r in records}
    assert np.array_equal(by_sequence[0].rgb, first) and np.array_equal(by_sequence[1].rgb, second)
    assert np.array_equal(by_sequence[2].rgb, first) and by_sequence[0].id != by_sequence[2].id
    assert by_sequence[1].occurrences[0].record_offset is not None


def test_pins_are_preserved_and_excluded(tmp_path):
    sc = synthetic.scenario(10, 6, seed=3)
    source = tmp_path / "Cache0000.bin"
    synthetic.write_cache(source, sc)
    data = ingest([source])
    first = stitch(data.tiles)
    key = first.instances[0].id
    pin = Placement(key, -640, -640, None, None, True)
    second = stitch(data.tiles, fixed={key: pin})
    assert second.placements[key] == pin
    assert all(p.x > -640 for k, p in second.placements.items() if k != key)
    assert not any(key in (link.a, link.b) for link in second.links)


def test_background_fill_creates_labelled_repeated_instance():
    image = Image.new("RGB", (192, 128), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in range(6, 60, 12):
        draw.line((0, y, 191, y), fill=(0, 0, 0), width=2)
    draw.line((10, 70, 10, 127), fill=(0, 0, 0), width=3)
    draw.line((181, 70, 181, 127), fill=(0, 0, 0), width=3)
    pixels = np.asarray(image)
    cells = {(0, 0): (0, 0), (1, 0): (1, 0), (2, 0): (2, 0), (0, 1): (0, 1), (2, 1): (2, 1)}
    rgbs = [pixels[y * 64 : (y + 1) * 64, x * 64 : (x + 1) * 64] for x, y in cells.values()]
    rgbs += [np.full((64, 64, 3), 255, np.uint8), np.full((64, 64, 3), 128, np.uint8)]
    records = [
        Record(Tile(str(i), np.ascontiguousarray(rgb), str(i), [], solid=i >= 5), "c", i, i >= 5)
        for i, rgb in enumerate(rgbs)
    ]
    models = (SeamModel.fallback(), SeamModel.fallback())
    scorer = Scorer(EdgeSet([r.tile.rgb for r in records]), models)
    blocks = Blocks(len(records))
    for index, cell in enumerate(cells):
        if index:
            blocks.merge(0, index, cell)
    fills = _fill_holes(records, blocks, scorer, EngineSettings(), 64, set(), None)
    assert len(fills) == 1
    instance, block, cell, confidence = fills[0]
    assert cell == (1, 1) and block == 0 and instance.id != records[5].tile.id
    assert np.array_equal(instance.rgb, records[5].tile.rgb) and confidence > 0.5
    assert FILL_REASON.startswith("background fill")


def test_contradicting_seam_counts_in_full():
    assert combined([3.0] * 9 + [-5.0]) < combined([3.0] * 9)
    assert combined([2.0] * 16) == pytest.approx(16)  # 32 tempered by sqrt(4/16)


def test_cancel_and_invalid_settings(tmp_path):
    sc = synthetic.scenario(6, 4, seed=1)
    source = tmp_path / "Cache0000.bin"
    synthetic.write_cache(source, sc)
    data = ingest([source])
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        stitch(data.tiles, cancel=cancelled)
    with pytest.raises(ValueError):
        stitch(data.tiles, EngineSettings(confidence=2))
    with pytest.raises(ValueError):
        stitch(data.tiles, EngineSettings(visual_prior=0))


def test_speculative_assembly_builds_larger_flagged_image():
    confident = run(seed=7, width_tiles=14, height_tiles=9)
    settings = EngineSettings(speculative=True)
    sc = synthetic.scenario(14, 9, seed=7)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder) / "Cache0000.bin"
        synthetic.write_cache(cache, sc)
        result = stitch(ingest([cache]).tiles, settings)
    metrics = adjacency_metrics(result, result.instances, sc)
    assert metrics["largest_region"] > confident["largest_region"]
    assert any(p.speculative for p in result.placements.values())
    assert any(link.reason.startswith("SPECULATIVE") for link in result.links)
    assert sum(r["speculative_records"] for r in result.settings["regions"]) > 0
