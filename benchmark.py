from __future__ import annotations

import argparse
import json
import platform
import tempfile
import time
from pathlib import Path

import numpy as np

import synthetic
from engine import EngineSettings, stitch
from evaluate import adjacency_metrics
from parser import ingest


def main():
    parser = argparse.ArgumentParser(
        description="Repeatable reconstruction accuracy/runtime and Qt heartbeat benchmark"
    )
    parser.add_argument("--screens", type=int, default=6, help="synthetic screens, one RDP8 cache file each")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.screens < 1:
        parser.error("At least one screen is required")
    result = {
        "screens": args.screens,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "dataset": (
            "Synthetic 1280x768 Windows-like screens (wallpaper, taskbar, Explorer, dense console text) with a "
            "scrolled/moved second state per screen; RDP8 v6 caches with cache-hit deduplication and newest-first "
            "record order. Accuracy on real caches is not established by this result."
        ),
    }
    with tempfile.TemporaryDirectory(prefix="rdp-stitcher-benchmark-") as temp:
        directory = Path(temp) / "caches"
        directory.mkdir()
        states = []
        records = 0
        for index in range(args.screens):
            scenario = synthetic.scenario(seed=100 + index)
            records += len(synthetic.write_cache(directory / f"Cache{index:04d}.bin", scenario, seed=index))
            states.extend(scenario.states)
        start = time.perf_counter()
        data = ingest([directory])
        result["ingest_seconds"] = time.perf_counter() - start
        assembly = stitch(data.tiles, EngineSettings())
        result["records"] = records
        result["stitch_seconds"] = assembly.elapsed_seconds
        metrics = adjacency_metrics(assembly, assembly.instances, synthetic.Scenario(states))
        result.update({key: metrics[key] for key in metrics})
        if args.gui:
            from PyQt6.QtCore import QTimer
            from PyQt6.QtWidgets import QApplication

            from gui import MainWindow

            app = QApplication.instance() or QApplication([])
            window = MainWindow(Path(temp) / "audit")
            window.show()
            beats = []
            timer = QTimer()
            timer.timeout.connect(lambda: beats.append(time.perf_counter()))
            timer.start(16)
            start = time.perf_counter()
            window.import_paths([str(directory)])
            while window._busy or not window.assembly.instances:
                app.processEvents()
                time.sleep(0.001)
                if time.perf_counter() - start > 180:
                    raise TimeoutError("GUI benchmark exceeded three minutes")
            result["gui_total_seconds"] = time.perf_counter() - start
            if len(beats) > 1:
                gaps = np.diff(beats) * 1000
                result["qt_heartbeat_ms"] = {
                    "median": float(np.median(gaps)),
                    "p95": float(np.percentile(gaps, 95)),
                    "max": float(gaps.max()),
                }
            result["gui_items"] = len(window.items)
            result["gui_measurement"] = "Offscreen Qt event-loop heartbeat; not a GPU/display frame-rate guarantee"
            timer.stop()
            window.close()
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
