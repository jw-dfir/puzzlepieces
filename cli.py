from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from audit import AuditLog
from engine import EngineSettings, stitch
from exporter import export_png, export_report
from layouts import native_bitmaps
from models import Cancelled
from parser import ingest


def main(argv=None):
    defaults = EngineSettings()
    parser = argparse.ArgumentParser(description="Headless RDP cache reconstruction and forensic export")
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--confidence", type=float, default=defaults.confidence, help="minimum join posterior")
    parser.add_argument("--visual-prior", type=float, default=defaults.visual_prior)
    parser.add_argument("--candidates", type=int, default=defaults.candidates)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--include-background", action="store_true", help="match uniform records like others")
    parser.add_argument("--no-fill", action="store_true", help="do not fill bounded holes with uniform records")
    parser.add_argument("--no-sequence", action="store_true", help="ignore cache order entirely")
    parser.add_argument(
        "--speculative", action="store_true", help="after confident joins, keep joining best fits (flagged guesses)"
    )
    parser.add_argument("--min-region", type=int, default=2, help="export regions with at least this many records")
    parser.add_argument(
        "--native", action="store_true", help="also export native cached bitmaps (not a reconstruction)"
    )
    parser.add_argument("--scale", type=int, default=1)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    audit = AuditLog(args.output / f"{label}.audit.jsonl")
    cancel = Event()
    try:
        data = ingest(args.inputs, workers=args.workers, cancel=cancel)
        audit.record(
            "import_completed",
            sources=data.sources,
            warnings=data.warnings,
            raw=data.raw_count,
            unique=len(data.tiles),
            duplicates=data.duplicate_count,
            suppressed=data.suppressed_count,
        )
        settings = EngineSettings(
            confidence=args.confidence,
            visual_prior=args.visual_prior,
            candidates=args.candidates,
            workers=args.workers,
            include_background=args.include_background,
            fill_backgrounds=not args.no_fill,
            use_sequence=not args.no_sequence,
            speculative=args.speculative,
        )
        result = stitch(data.tiles, settings, cancel=cancel)
        audit.record(
            "stitch_completed",
            settings=asdict(settings),
            seam_models=result.settings.get("seam_models"),
            cache_order_priors=result.settings.get("cache_order_priors"),
            evidence=result.settings.get("evidence_counts"),
            links=[asdict(link) for link in result.links],
        )
        for placement in result.placements.values():
            audit.record("tile_placed", **asdict(placement))
        outputs = []
        regions = [r for r in result.settings.get("regions", []) if r["records"] >= args.min_region]
        region_dir = args.output / f"{label}.regions"
        for region in regions:
            subset = {key: p for key, p in result.placements.items() if p.cluster == region["region"]}
            outputs.append(
                export_png(
                    region_dir / f"region-{region['region']:04d}-{region['records']}rec.png",
                    result.instances,
                    subset,
                    scale=args.scale,
                )
            )
        if result.placements:
            outputs.append(
                export_png(
                    args.output / f"{label}.all-regions.png", result.instances, result.placements, scale=args.scale
                )
            )
        if args.native:
            instances, layout = native_bitmaps(data, cancel)
            outputs.append(
                export_png(
                    args.output / f"{label}.native-bitmaps.png",
                    instances,
                    layout.placements,
                    scale=args.scale,
                    layout_kind="native_bitmaps",
                )
            )
        for output in outputs:
            audit.record("png_export", **{k: v for k, v in output.items() if k != "tiles"})
        audit.flush()
        report = args.output / f"{label}.report.json"
        export_report(report, data, result, audit.snapshot(), outputs)
        joined = sum(r["records"] for r in result.settings.get("regions", []))
        print(
            f"{result.settings.get('records', 0):,} native records; {joined:,} joined into "
            f"{len(result.settings.get('regions', [])):,} inferred regions; "
            f"{len(result.unplaced):,} left for review; {result.elapsed_seconds:.1f}s"
        )
        for model in result.settings.get("seam_models", []):
            print(f"Seam model: {model}")
        print(f"Region PNGs: {region_dir if regions else 'none'}")
        print(f"Report: {report}")
        for warning in data.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return 0 if data.tiles else 2
    except KeyboardInterrupt:
        cancel.set()
        return 130
    except (ValueError, OSError, Cancelled) as error:
        audit.record("error", detail=str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
