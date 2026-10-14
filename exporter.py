from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import zlib
from dataclasses import asdict
from pathlib import Path
from threading import Event

import numpy as np
from PIL import Image, PngImagePlugin

from models import Assembly, IngestResult, Placement, Tile, check_cancel, tile_record

MAX_EXPORT_PIXELS = 100_000_000
MAX_EXPORT_TOTAL_PIXELS = 1_000_000_000


def _stream_png(path, tiles, placements, box, scale, info, cancel):
    left, top, right, bottom = box
    width, height = (right - left) * scale, (bottom - top) * scale
    starts = {}
    for key, p in placements.items():
        tile = tiles.get(key)
        if tile is not None and p.x < right and p.x + tile.width > left and p.y < bottom and p.y + tile.height > top:
            starts.setdefault(max(top, p.y), []).append((tile, p))
    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")

        def chunk(kind, data):
            stream.write(
                struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        for kind, data, *_ in info.chunks:
            chunk(kind, data)
        compressor = zlib.compressobj(6)
        active = []
        for y in range(top, bottom):
            check_cancel(cancel)
            active = [(tile, p) for tile, p in active if y < p.y + tile.height]
            active.extend(starts.get(y, []))
            row = bytearray(width * 4)
            for tile, p in active:
                x0, x1 = max(left, p.x), min(right, p.x + tile.width)
                rgb = tile.rgb[y - p.y, x0 - p.x : x1 - p.x]
                rgba = np.empty((len(rgb), 4), dtype=np.uint8)
                rgba[:, :3], rgba[:, 3] = rgb, 255
                if scale > 1:
                    rgba = np.repeat(rgba, scale, axis=0)
                row[(x0 - left) * scale * 4 : (x1 - left) * scale * 4] = rgba.tobytes()
            scanline = b"\x00" + row
            for _ in range(scale):
                encoded = compressor.compress(scanline)
                if encoded:
                    chunk(b"IDAT", encoded)
        chunk(b"IDAT", compressor.flush())
        chunk(b"IEND", b"")


def bounds(tiles: dict[str, Tile], placements: dict[str, Placement]) -> tuple[int, int, int, int]:
    valid = [(tiles[key], p) for key, p in placements.items() if key in tiles]
    if not valid:
        raise ValueError("No placed tiles to export")
    return (
        min(p.x for _, p in valid),
        min(p.y for _, p in valid),
        max(p.x + t.width for t, p in valid),
        max(p.y + t.height for t, p in valid),
    )


def _atomic_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def export_png(
    path: str | Path,
    tiles: list[Tile],
    placements: dict[str, Placement],
    *,
    crop: tuple[int, int, int, int] | None = None,
    scale: int = 1,
    cancel: Event | None = None,
    layout_kind: str = "inferred",
) -> dict:
    if not isinstance(scale, int) or not 1 <= scale <= 8:
        raise ValueError("Export scale must be an integer within 1–8")
    by_id = {t.id: t for t in tiles}
    box = crop or bounds(by_id, placements)
    left, top, right, bottom = map(int, box)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError("Empty export rectangle")
    pixels = width * height * scale * scale
    if pixels > MAX_EXPORT_TOTAL_PIXELS or width * scale > 250000 or height * scale > 250000:
        raise ValueError("Export dimensions are excessive; use the all-tiles contact sheet or crop a smaller area")
    check_cancel(cancel)
    streaming = pixels > MAX_EXPORT_PIXELS
    image = None if streaming else Image.new("RGBA", (width, height), (0, 0, 0, 0))
    exported = []
    for key, p in placements.items():
        check_cancel(cancel)
        tile = by_id.get(key)
        if tile is None or p.x >= right or p.y >= bottom or p.x + tile.width <= left or p.y + tile.height <= top:
            continue
        if image is not None:
            image.paste(Image.fromarray(tile.rgb).convert("RGBA"), (p.x - left, p.y - top))
        exported.append(key)
    if scale > 1 and image is not None:
        image = image.resize((width * scale, height * scale), Image.Resampling.NEAREST)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "Puzzle Pieces by jwdfir")
    interpretation = {
        "inferred": (
            "Inferred reconstruction: each region is joined from cached records by calibrated seam evidence and "
            "measured cache-order structure; regions are packed side by side and their relative placement, original "
            "screen coordinates and capture time are unknown; gaps are transparent; confidences are model posteriors"
        ),
        "contact_sheet": "Contact sheet / tile index only; NOT a reconstruction; positions do not imply adjacency",
        "native_bitmaps": "Native cached bitmaps restored individually; NOT a reconstruction; packing is a contact sheet",
    }
    info.add_text("Interpretation", interpretation.get(layout_kind, interpretation["inferred"]))
    info.add_text("SceneBounds", json.dumps([left, top, right, bottom]))
    info.add_text("LayoutKind", layout_kind)
    fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".png")
    os.close(fd)
    try:
        if streaming:
            _stream_png(temporary, by_id, placements, (left, top, right, bottom), scale, info, cancel)
        else:
            image.save(temporary, format="PNG", pnginfo=info, compress_level=6)
        check_cancel(cancel)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "path": str(target),
        "sha256": digest,
        "bounds": [left, top, right, bottom],
        "scale": scale,
        "pixels": [width * scale, height * scale],
        "tile_ids": exported,
        "layout_kind": layout_kind,
        "streaming": streaming,
        "placements": [asdict(placements[key]) for key in exported],
        "tiles": [tile_record(by_id[key]) for key in exported] if layout_kind != "contact_sheet" else [],
    }


def manifest(ingested: IngestResult, assembly: Assembly, events: list[dict], exports: list[dict] | None = None) -> dict:
    return {
        "schema": "rdp-stitcher/1",
        "version": "1.2.0",
        "interpretation": (
            "Regions are inferred joins of native cached record instances, not proven screen captures or sessions. "
            "Confidences are posteriors of a seam model calibrated on seams inside this evidence's own records; they "
            "are heuristic, not validated error rates. Cache order is used only to the extent its adjacency rate was "
            "measured on the evidence. Cache sequence and file mtime are not capture timestamps. Deduplication is a "
            "storage optimisation: every record occurrence is retained, and background fills are labelled as "
            "repeated instances of a cached tile."
        ),
        "sources": ingested.sources,
        "warnings": ingested.warnings,
        "counts": {
            "raw": ingested.raw_count,
            "unique": len(ingested.tiles),
            "duplicates": ingested.duplicate_count,
            "suppressed": ingested.suppressed_count,
        },
        "tiles": [tile_record(t) for t in ingested.tiles],
        "instances": [tile_record(t) for t in assembly.instances],
        "regions": assembly.settings.get("regions", []),
        "placements": [asdict(p) for p in assembly.placements.values()],
        "links": [asdict(link) for link in assembly.links],
        "unplaced": assembly.unplaced,
        "settings": assembly.settings,
        "elapsed_seconds": assembly.elapsed_seconds,
        "exports": exports or [],
        "audit": events,
    }


def export_report(
    path: str | Path, ingested: IngestResult, assembly: Assembly, events: list[dict], exports: list[dict] | None = None
) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(target, manifest(ingested, assembly, events, exports))
