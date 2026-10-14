import json

import numpy as np
import pytest
from PIL import Image

from audit import AuditLog, verify_audit
from exporter import export_png, export_report
from helpers import puzzle
from models import Assembly, IngestResult, Placement


def test_png_exact_pixels_scale_crop_and_report(tmp_path):
    expected, tiles = puzzle(2, 2)
    result = Assembly({t.id: Placement(t.id, 96 + i % 2 * 32, 64 + i // 2 * 32, 0.97, 1) for i, t in enumerate(tiles)})
    p = result.placements[tiles[0].id]
    output = export_png(tmp_path / "image.png", tiles, result.placements)
    with Image.open(output["path"]) as image:
        assert image.mode == "RGBA"
        assert np.array_equal(np.array(image)[:, :, :3], expected)
    crop = (p.x + 4, p.y + 5, p.x + 20, p.y + 18)
    output = export_png(tmp_path / "crop.png", tiles, result.placements, crop=crop, scale=2)
    assert output["pixels"] == [32, 26]
    with Image.open(output["path"]) as image:
        assert np.array_equal(np.array(image)[::2, ::2, :3], expected[5:18, 4:20])
    export_report(tmp_path / "report.json", IngestResult(tiles=tiles), result, [], [output])
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["schema"] == "rdp-stitcher/1" and len(report["placements"]) == 4
    assert report["exports"][0]["sha256"] == output["sha256"]


def test_negative_coordinates_transparent_gaps_and_limits(tmp_path):
    _, tiles = puzzle(1, 2)
    placements = {
        tiles[0].id: Placement(tiles[0].id, -32, -64, None, None),
        tiles[1].id: Placement(tiles[1].id, 32, -64, None, None),
    }
    output = export_png(tmp_path / "negative.png", tiles, placements)
    with Image.open(output["path"]) as image:
        assert image.size == (96, 32)
        assert np.all(np.array(image)[:, 32:64, 3] == 0)
    with pytest.raises(ValueError):
        export_png(tmp_path / "bad.png", tiles, placements, crop=(0, 0, 100000, 100000))
    with pytest.raises(ValueError):
        export_png(tmp_path / "bad.png", tiles, placements, scale=0)


def test_audit_detects_tampering(tmp_path):
    path = tmp_path / "events.jsonl"
    log = AuditLog(path)
    log.record("tile_placed", tile_id="abc", x=32, y=-64, confidence=0.942)
    log.close()
    assert verify_audit(path)
    text = path.read_text().replace('"x": 32', '"x": 64')
    path.write_text(text)
    assert not verify_audit(path)
