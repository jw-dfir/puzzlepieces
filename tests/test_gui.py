import time

import pytest
from PIL import Image
from PyQt6.QtCore import QPointF, QTimer
from PyQt6.QtWidgets import QApplication

import synthetic
from gui import MainWindow
from helpers import puzzle


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, predicate, timeout=15):
    start = time.monotonic()
    while not predicate():
        app.processEvents()
        time.sleep(0.001)
        if time.monotonic() - start > timeout:
            raise AssertionError("Qt operation timed out")


def test_auto_import_stitch_regions_manual_pin_and_undo(app, tmp_path):
    input_dir = tmp_path / "cache"
    input_dir.mkdir()
    synthetic.write_cache(input_dir / "Cache0000.bin", synthetic.scenario(14, 9, seed=7))
    window = MainWindow(tmp_path / "audit")
    window.show()
    beats = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: beats.append(time.perf_counter()))
    heartbeat.start(5)
    window.import_paths([str(input_dir)])
    pump(app, lambda: window.assembly.instances and len(window.items) == len(window.assembly.placements), 120)
    pump(app, lambda: not window._busy, 60)
    assert window.assembly.settings["layout_kind"] == "inferred"
    assert window.assembly.settings["regions"] and window.region_list.count() == len(
        window.assembly.settings["regions"]
    )
    assert all(t.width == t.height == 64 for t in window.by_id.values())
    assert window.model.rowCount() >= 1  # uncertain records stay in the sidebar for review
    window.region_list.setCurrentRow(0)
    cluster = window.region_list.item(0).data(256)
    assert window.cluster_combo.currentData() == cluster
    selected = {item.tile_id for item in window.scene.selectedItems()}
    assert selected == {k for k, p in window.assembly.placements.items() if p.cluster == cluster}
    window.scene.clearSelection()
    key = next(iter(window.items))
    window.place_tile(key, QPointF(-640, -640))
    assert window.assembly.placements[key].manual
    window.run_stitch()
    pump(app, lambda: not window._busy, 120)
    assert (window.assembly.placements[key].x, window.assembly.placements[key].y) == (-640, -640)
    window.place_tile(key, QPointF(-1280, -1280))
    window.undo()
    assert (window.items[key].pos().x(), window.items[key].pos().y()) == (-640, -640)
    window.items[key].setSelected(True)
    window.show_properties()
    assert "Native cached record instance" in window.properties.text() or "Manual" in window.properties.text()
    window.unplace_selected()
    assert key not in window.items and window.model.rowCount() >= 1
    window.undo()
    assert key in window.items
    heartbeat.stop()
    assert beats
    window.close()


def test_multi_selection_move_updates_all_and_rejects_collision(app, tmp_path):
    _, tiles = puzzle(1, 3)
    directory = tmp_path / "input"
    directory.mkdir()
    for i, tile in enumerate(tiles):
        Image.fromarray(tile.rgb).save(directory / f"tile{i}.bmp")
    window = MainWindow(tmp_path / "audit")
    window.auto.setChecked(False)
    window.import_paths([str(directory)])
    pump(app, lambda: len(window.by_id) == 3 and not window._busy)
    keys = list(window.by_id)
    for i, key in enumerate(keys):
        window.place_tile(key, QPointF(i * 32, 0))
    window.items[keys[0]].setPos(0, 32)
    window.items[keys[1]].setPos(32, 32)
    window.tile_moved(window.items[keys[0]], QPointF(0, 0))
    assert window.assembly.placements[keys[0]].y == window.assembly.placements[keys[1]].y == 32
    window.items[keys[0]].setPos(64, 0)
    window.tile_moved(window.items[keys[0]], QPointF(0, 32))
    assert window.items[keys[0]].pos() == QPointF(0, 32)
    window.close()


def test_worker_cancellation_preserves_workspace(app, tmp_path):
    window = MainWindow(tmp_path / "audit")

    def task(cancel, progress):
        from models import check_cancel

        for i in range(1000):
            check_cancel(cancel)
            time.sleep(0.001)
        return None

    window._start(task, lambda _: None)
    window.cancel_task()
    pump(app, lambda: not window._busy)
    assert not window.worker and not window.assembly.placements
    window.close()


def test_gui_contact_native_and_export_without_inferred_links(app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    directory = tmp_path / "input"
    directory.mkdir()
    Image.new("RGB", (64, 64), (40, 50, 60)).save(directory / "native.bmp")
    window = MainWindow(tmp_path / "audit")
    window.auto.setChecked(False)
    window.import_paths([str(directory)])
    pump(app, lambda: bool(window.ingested.tiles) and not window._busy)
    target = tmp_path / "all.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), "PNG"))
    window.export()
    pump(app, lambda: not window._busy)
    assert target.exists() and target.with_suffix(".report.json").exists()
    with Image.open(target) as image:
        assert image.info["LayoutKind"] == "contact_sheet"
    window.export_mode.setCurrentIndex(5)
    window.export()
    pump(app, lambda: not window._busy)
    window.layout_view.setCurrentIndex(2)
    pump(app, lambda: not window._busy)
    assert len(window.items) == 4
    assert window.model.rowCount() == 0
    window.export_mode.setCurrentIndex(0)
    window.export()
    pump(app, lambda: not window._busy)
    with Image.open(target) as image:
        assert image.size == (64, 64) and image.getpixel((33, 33)) == (40, 50, 60, 255)
    window.layout_view.setCurrentIndex(0)
    pump(app, lambda: not window._busy)
    assert not window.items
    window.close()


def test_speculative_outline_clears_on_rerun_move_and_undo(app, tmp_path):
    # Regression test: a reused TileItem (same key across re-stitches) must drop its speculative outline when the
    # new placement is no longer speculative, and a manual move/undo must keep the outline consistent with the data.
    input_dir = tmp_path / "cache"
    input_dir.mkdir()
    synthetic.write_cache(input_dir / "Cache0000.bin", synthetic.scenario(14, 9, seed=7))
    window = MainWindow(tmp_path / "audit")
    window.speculative.setChecked(True)
    window.import_paths([str(input_dir)])
    pump(
        app,
        lambda: window.assembly.instances and not window._busy and len(window.items) == len(window.assembly.placements),
    )
    speculative_keys = [k for k, p in window.assembly.placements.items() if p.speculative]
    assert speculative_keys, "fixture should produce at least one speculative placement to test against"
    key = speculative_keys[0]
    assert window.items[key].speculative

    # Re-run without speculative mode: that tile, if still placed, must no longer be flagged.
    window.speculative.setChecked(False)
    window.run_stitch()
    pump(app, lambda: not window._busy)
    if key in window.assembly.placements:
        assert not window.assembly.placements[key].speculative
        assert window.items[key].speculative is False

    # Re-enable speculative mode, re-run, then manually move a speculative tile: the outline must clear.
    window.speculative.setChecked(True)
    window.run_stitch()
    pump(app, lambda: not window._busy)
    speculative_keys = [k for k, p in window.assembly.placements.items() if p.speculative]
    assert speculative_keys
    key = speculative_keys[0]
    window.place_tile(key, QPointF(-2000, -2000))
    assert window.items[key].speculative is False
    assert not window.assembly.placements[key].speculative

    window.undo()
    assert window.items[key].speculative == window.assembly.placements[key].speculative
    window.close()
