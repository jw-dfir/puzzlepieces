"""Measure reconstruction accuracy against synthetic ground truth.

    python evaluate.py --out evaluation           # default scenarios, JSON + PNG of the regions
    python evaluate.py --drop 0.1 --seed 3

An adjacency in the output is *correct* when, in at least one ground-truth screen state, the two tiles' pixels occur
at exactly that relative offset. This judges pixels rather than record identity, so a repeated background tile placed
by hole filling counts as correct wherever its pixels truly belong. Recall counts distinct true adjacencies between
non-uniform stored tiles that the output reproduces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np

import synthetic
from engine import EngineSettings, stitch
from models import Assembly, Tile
from parser import ingest


def _digest(rgb: np.ndarray) -> bytes:
    return hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).digest()


def adjacency_metrics(
    assembly: Assembly, tiles: list[Tile], sc: synthetic.Scenario, tile: int = synthetic.TILE
) -> dict:
    by_id = {t.id: t for t in tiles}
    index = synthetic.screen_index(sc, tile)
    positions = [{h: set(v) for h, v in state.items()} for state in index]
    stored = {_digest(t.rgb) for t in tiles if t.width == t.height == tile}
    flat = {_digest(t.rgb) for t in tiles if t.width == t.height == tile and t.solid}
    truth = set()
    for state in index:
        grid = {p: h for h, ps in state.items() for p in ps}
        for (x, y), h in grid.items():
            for d in ((1, 0), (0, 1)):
                other = grid.get((x + d[0], y + d[1]))
                if other is not None and h in stored and other in stored and h not in flat and other not in flat:
                    truth.add((h, other, d))
    cells = {}
    for key, p in assembly.placements.items():
        t = by_id.get(key)
        if t is None or t.width != tile or t.height != tile or p.x % tile or p.y % tile:
            continue
        cells[p.x // tile, p.y // tile] = (_digest(t.rgb), p.cluster)
    correct = wrong = 0
    found = set()
    wrong_by_cluster = defaultdict(int)
    size_by_cluster = defaultdict(int)
    for (x, y), (h, cluster) in cells.items():
        size_by_cluster[cluster] += 1
        for d in ((1, 0), (0, 1)):
            neighbour = cells.get((x + d[0], y + d[1]))
            if neighbour is None or neighbour[1] != cluster or cluster is None:
                continue
            ok = any(
                (px + d[0], py + d[1]) in state.get(neighbour[0], ())
                for state in positions
                for px, py in state.get(h, ())
            )
            if ok:
                correct += 1
                found.add((h, neighbour[0], d))
            else:
                wrong += 1
                wrong_by_cluster[cluster] += 1
    clusters = [c for c in size_by_cluster if c is not None]
    coherent = [c for c in clusters if size_by_cluster[c] >= 4 and not wrong_by_cluster[c]]
    total = correct + wrong
    return {
        "adjacencies": total,
        "correct": correct,
        "false_joins": wrong,
        "precision": round(correct / total, 4) if total else None,
        "true_adjacencies": len(truth),
        "recall": round(len(found & truth) / len(truth), 4) if truth else None,
        "regions": len(clusters),
        "largest_region": max((size_by_cluster[c] for c in clusters), default=0),
        "coherent_regions_ge4": len(coherent),
        "tiles_in_coherent_regions": sum(size_by_cluster[c] for c in coherent),
        "tiles_placed": len(cells),
    }


def run(
    seed: int = 7,
    drop: float = 0.0,
    settings: EngineSettings | None = None,
    out: Path | None = None,
    width_tiles: int = 20,
    height_tiles: int = 12,
) -> dict:
    sc = synthetic.scenario(width_tiles, height_tiles, seed=seed)
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder) / "Cache0000.bin"
        truth = synthetic.write_cache(cache, sc, seed=seed, drop=drop)
        data = ingest([cache])
    result = stitch(data.tiles, settings)
    metrics = adjacency_metrics(result, result.instances, sc)
    metrics.update(records=len(truth), seed=seed, drop=drop, seconds=round(result.elapsed_seconds, 2))
    metrics["seam_models"] = result.settings["seam_models"]
    if out:
        from exporter import export_png

        out.mkdir(parents=True, exist_ok=True)
        for summary in result.settings["regions"][:20]:
            subset = {k: p for k, p in result.placements.items() if p.cluster == summary["region"]}
            export_png(out / f"seed{seed}-region{summary['region']:03d}.png", result.instances, subset)
        for state, screen in enumerate(sc.states):
            from PIL import Image

            Image.fromarray(screen).save(out / f"seed{seed}-truth-state{state}.png")
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure correct/false joins on synthetic ground truth")
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 11, 23])
    parser.add_argument("--drop", type=float, default=0.0)
    parser.add_argument("--confidence", type=float, default=EngineSettings().confidence)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    settings = EngineSettings(confidence=args.confidence)
    results = [run(seed, args.drop, settings, args.out) for seed in args.seeds]
    report = {"settings": asdict(settings), "results": results}
    print(json.dumps(report, indent=2))
    if args.out:
        (args.out / "evaluation.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
