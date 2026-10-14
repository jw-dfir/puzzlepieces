from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import BinaryIO, Iterator

import numpy as np
from PIL import Image

from models import IngestResult, Occurrence, Progress, Tile, check_cancel

BIN_MAGIC = b"RDP8bmp\x00"
PALETTE = np.asarray(json.loads(Path(__file__).with_name("palette.json").read_text()), dtype=np.uint8)


class CacheFormatError(ValueError):
    pass


def natural_key(path: Path) -> tuple:
    return tuple((1, int(p)) if p.isdigit() else (0, p.casefold()) for p in re.split(r"(\d+)", str(path)))


def decode_interleaved(data: bytes, width: int, height: int, bpp: int) -> bytes:
    if bpp not in (1, 2, 3, 4) or not 0 < width <= 64 or not 0 < height <= 64:
        raise CacheFormatError("Invalid RLE dimensions/depth")
    count = width * height
    out: list[int] = []
    pos = 0
    foreground = (1 << (8 * bpp)) - 1
    insert_fg = False
    first_line = True

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise CacheFormatError("Truncated RLE order")
        value = data[pos : pos + n]
        pos += n
        return value

    def pixel() -> int:
        return int.from_bytes(take(bpp), "little")

    def emit(value: int) -> None:
        if len(out) >= count:
            raise CacheFormatError("RLE output exceeds declared dimensions")
        out.append(value)

    def background() -> int:
        return out[-width] if len(out) >= width else 0

    while pos < len(data):
        if first_line and len(out) >= width:
            first_line = False
            insert_fg = False
        header = take(1)[0]
        if header >= 0xF0:
            code = header
            if code in (0xFD, 0xFE):
                run = 1
            elif code in (0xF9, 0xFA):
                run = 8
            elif code <= 0xF8 and code != 0xF5:
                run = int.from_bytes(take(2), "little")
            else:
                raise CacheFormatError(f"Unsupported RLE opcode 0x{code:02x}")
        elif header & 0xE0 == 0xA0:
            raise CacheFormatError("Reserved RLE opcode")
        else:
            regular = header < 0xC0
            mask = 0x1F if regular else 0x0F
            code = header & (0xE0 if regular else 0xF0)
            run = header & mask
            if code in (0x40, 0xD0):
                run = run * 8 if run else take(1)[0] + 1
            elif not run:
                run = take(1)[0] + (32 if regular else 16)
        multiplier = 2 if code in (0xE0, 0xF8) else 1
        if run <= 0 or len(out) + run * multiplier > count:
            raise CacheFormatError("Invalid RLE run length")
        if code in (0x00, 0xF0):
            if insert_fg:
                emit(background() ^ foreground)
                run -= 1
            for _ in range(run):
                emit(background())
            insert_fg = True
            continue
        insert_fg = False
        if code in (0xC0, 0xF6, 0xD0, 0xF7):
            foreground = pixel()
        if code in (0x20, 0xC0, 0xF1, 0xF6):
            for _ in range(run):
                emit(background() ^ foreground)
        elif code in (0x40, 0xD0, 0xF2, 0xF7, 0xF9, 0xFA):
            masks = bytes([3 if code == 0xF9 else 5]) if code in (0xF9, 0xFA) else take((run + 7) // 8)
            for i in range(run):
                emit(background() ^ (foreground if masks[i // 8] & (1 << (i % 8)) else 0))
        elif code in (0x60, 0xF3):
            colour = pixel()
            for _ in range(run):
                emit(colour)
        elif code in (0x80, 0xF4):
            for _ in range(run):
                emit(pixel())
        elif code in (0xE0, 0xF8):
            a, b = pixel(), pixel()
            for _ in range(run):
                emit(a)
                emit(b)
        elif code in (0xFD, 0xFE):
            emit((1 << (8 * bpp)) - 1 if code == 0xFD else 0)
        else:
            raise CacheFormatError(f"Unsupported RLE opcode 0x{code:02x}")
    if len(out) != count:
        raise CacheFormatError(f"RLE yielded {len(out)} pixels; expected {count}")
    return b"".join(p.to_bytes(bpp, "little") for p in out)


def pixels_to_rgb(data: bytes, width: int, height: int, bpp: int, bottom_up: bool) -> np.ndarray:
    if len(data) != width * height * bpp:
        raise CacheFormatError("Pixel byte count disagrees with tile dimensions")
    if bpp in (3, 4):
        array = np.frombuffer(data, np.uint8).reshape(height, width, bpp)[:, :, :3][:, :, ::-1]
    elif bpp == 2:
        p = np.frombuffer(data, "<u2").reshape(height, width)
        r, g, b = (p >> 11) & 31, (p >> 5) & 63, p & 31
        array = np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)), axis=2).astype(np.uint8)
    elif bpp == 1:
        array = PALETTE[np.frombuffer(data, np.uint8).reshape(height, width)]
    else:
        raise CacheFormatError(f"Unsupported depth: {8 * bpp}")
    return np.ascontiguousarray(array[::-1] if bottom_up else array)


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise CacheFormatError(f"Truncated record at offset {stream.tell() - len(data)}")
    return data


def _valid_header(header: bytes) -> bool:
    if len(header) != 20:
        return False
    _, _, width, height, length, flags = struct.unpack("<IIHHII", header)
    return 0 < width <= 64 and 0 < height <= 64 and 0 < length <= 16384 and flags < 0x10000


def _infer_bpp(stream: BinaryIO, offset: int, header: bytes, name: str, file_size: int) -> int:
    match = re.search(r"bcache(2|22|23|24)\.bmc$", name, re.I)
    if match:
        return {"2": 1, "22": 2, "23": 3, "24": 4}[match[1]]
    _, _, width, height, length, flags = struct.unpack("<IIHHII", header)
    if not flags & 8 and width * height and length % (width * height) == 0:
        bpp = length // (width * height)
        if bpp in (1, 2, 3, 4):
            return bpp
    candidates = []
    saved = stream.tell()
    for bpp in (1, 2, 3, 4):
        next_offset = offset + 20 + 4096 * bpp
        if next_offset == file_size:
            candidates.append(bpp)
        elif next_offset + 20 <= file_size:
            stream.seek(next_offset)
            if _valid_header(stream.read(20)):
                candidates.append(bpp)
    stream.seek(saved)
    if len(candidates) != 1:
        raise CacheFormatError("Ambiguous BMC slot depth; retain the original bcache2/22/23/24.bmc filename")
    return candidates[0]


def iter_cache(path: Path, cancel: Event | None = None) -> Iterator[tuple[np.ndarray, int, int, str]]:
    size = path.stat().st_size
    with path.open("rb") as stream:
        magic = stream.read(8)
        is_bin = magic == BIN_MAGIC
        if is_bin:
            version = struct.unpack("<I", _read_exact(stream, 4))[0]
            if version not in (1, 6):
                raise CacheFormatError(f"Unsupported RDP8bmp version {version}")
        else:
            if path.suffix.lower() == ".bin":
                raise CacheFormatError("BIN input lacks the RDP8bmp signature")
            stream.seek(0)
        bpp = 0
        sequence = 0
        while stream.tell() < size:
            check_cancel(cancel)
            offset = stream.tell()
            header = _read_exact(stream, 12 if is_bin else 20)
            key1, key2, width, height = struct.unpack("<IIHH", header[:12])
            if not any(header):
                if bpp and not is_bin:
                    _read_exact(stream, 4096 * bpp)
                    sequence += 1
                    continue
                if any(stream.read()):
                    raise CacheFormatError(f"Empty header followed by unknown data at {offset}")
                break
            limit = 256 if is_bin else 64
            if not 0 < width <= limit or not 0 < height <= limit:
                raise CacheFormatError(f"Invalid dimensions {width}×{height} at {offset}")
            if is_bin:
                payload = _read_exact(stream, width * height * 4)
                rgb = pixels_to_rgb(payload, width, height, 4, False)
            else:
                length, flags = struct.unpack("<II", header[12:])
                bpp = bpp or _infer_bpp(stream, offset, header, path.name, size)
                if not 0 < length <= 4096 * bpp:
                    raise CacheFormatError(f"Invalid BMC payload length {length} at {offset}")
                slot = _read_exact(stream, 4096 * bpp)
                payload = slot[:length]
                if flags & 8:
                    payload = decode_interleaved(payload, width, height, bpp)
                rgb = pixels_to_rgb(payload, width, height, bpp, True)
            yield rgb, sequence, offset, f"{key1:08x}{key2:08x}"
            sequence += 1


def _hash_source(path: Path, cancel: Event | None) -> tuple[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel(cancel)
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def _sidecar(path: Path) -> dict:
    metadata_path = path.parent / "tile_metadata.json"
    if not metadata_path.exists():
        return {}
    content = json.loads(metadata_path.read_text(encoding="utf-8"))
    entry = content.get(path.name, {})
    if not isinstance(entry, dict):
        raise ValueError("Metadata entry must be an object")
    return entry


def _ingest_file(path: Path, cancel: Event | None) -> tuple[list[Tile], dict, list[str]]:
    warnings: list[str] = []
    tiles: list[Tile] = []
    before = path.stat()
    md5, sha256 = _hash_source(path, cancel)
    source = {"path": str(path), "sha256": sha256, "md5": md5, "size": before.st_size, "mtime_ns": before.st_mtime_ns}
    if before.st_size == 0:
        return [], source, [f"{path}: empty source file"]
    try:
        metadata = _sidecar(path)
        timestamp = metadata.get("timestamp")
        if isinstance(timestamp, str):
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Sidecar timestamp must specify a timezone")
            timestamp = parsed.timestamp()
        elif timestamp is not None:
            timestamp = float(timestamp)
        if timestamp is not None and not math.isfinite(timestamp):
            raise ValueError("Sidecar timestamp must be finite")
        group = str(metadata["group"]) if "group" in metadata else None
    except (OSError, ValueError, TypeError, AttributeError) as error:
        timestamp, group = None, None
        warnings.append(f"{path}: ignored sidecar: {error}")
    nums = re.findall(r"\d+", path.stem)
    file_sequence = int(nums[-1]) if nums else 0
    base = Occurrence(str(path), sha256, md5, file_sequence, before.st_mtime_ns, timestamp, group)
    try:
        if path.suffix.lower() == ".bmp":
            with Image.open(path) as image:
                if image.format != "BMP" or not 0 < image.width <= 256 or not 0 < image.height <= 256:
                    raise CacheFormatError("Expected BMP dimensions within 1–256 pixels")
                records = [(np.array(image.convert("RGB")), file_sequence, None, None)]
        else:
            records = iter_cache(path, cancel)
        for rgb, sequence, offset, key in records:
            check_cancel(cancel)
            height, width = rgb.shape[:2]
            for y in range(0, height, 32):
                for x in range(0, width, 32):
                    part = np.ascontiguousarray(rgb[y : y + 32, x : x + 32])
                    data = part.tobytes()
                    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
                    identity = hashlib.sha256(struct.pack("<HH", part.shape[1], part.shape[0]) + data).hexdigest()
                    solid = bool(np.max(np.ptp(part.astype(np.int16).reshape(-1, 3), axis=0)) <= 2)
                    occurrence = replace(
                        base,
                        sequence=sequence,
                        record_offset=offset,
                        cache_key=key,
                        native_width=width,
                        native_height=height,
                        sub_x=x,
                        sub_y=y,
                    )
                    part.flags.writeable = False
                    tiles.append(Tile(identity, part, digest, [occurrence], solid))
    except (OSError, ValueError, struct.error, Image.DecompressionBombError) as error:
        warnings.append(f"{path}: {error}; parsing stopped, prior valid tiles retained")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise CacheFormatError(f"Source changed during ingestion: {path}; discarded")
    return tiles, source, warnings


def ingest(
    paths: list[str | Path],
    *,
    blank_frequency: int = 3,
    workers: int = 4,
    cancel: Event | None = None,
    progress: Progress | None = None,
) -> IngestResult:
    if blank_frequency < 1 or workers < 1:
        raise ValueError("blank_frequency/workers must be positive")
    result = IngestResult()
    candidates: set[Path] = set()
    for value in paths:
        check_cancel(cancel)
        path = Path(value).expanduser().resolve()
        if path.is_file():
            candidates.add(path)
        elif path.is_dir():
            for root, dirs, files in os.walk(
                path, onerror=lambda error: result.warnings.append(str(error)), followlinks=False
            ):
                check_cancel(cancel)
                dirs.sort()
                for name in files:
                    if Path(name).suffix.lower() in (".bmp", ".bin", ".bmc"):
                        candidates.add(Path(root, name).resolve())
        else:
            result.warnings.append(f"Input does not exist: {path}")
    files = sorted((p for p in candidates if p.suffix.lower() in (".bmp", ".bin", ".bmc")), key=natural_key)
    unique: dict[str, Tile] = {}

    def run(path: Path):
        try:
            return _ingest_file(path, cancel)
        except (OSError, ValueError) as error:
            return [], None, [f"{path}: {error}"]

    with ThreadPoolExecutor(max_workers=min(workers, 8), thread_name_prefix="rdp-ingest") as pool:
        for start in range(0, len(files), 64):
            check_cancel(cancel)
            for index, (tiles, source, warnings) in enumerate(pool.map(run, files[start : start + 64]), start + 1):
                check_cancel(cancel)
                if source:
                    result.sources.append(source)
                result.warnings.extend(warnings)
                for tile in tiles:
                    result.raw_count += 1
                    if tile.id in unique:
                        unique[tile.id].occurrences.extend(tile.occurrences)
                        result.duplicate_count += 1
                    else:
                        unique[tile.id] = tile
                if progress:
                    progress("Hashing and decoding", index, len(files))
    result.tiles = sorted(
        unique.values(),
        key=lambda t: (
            t.occurrences[0].group or str(Path(t.occurrences[0].source).parent),
            t.occurrences[0].timestamp or 0,
            t.occurrences[0].sequence,
            t.id,
        ),
    )
    result.colour_order = [
        t.id for t in sorted(result.tiles, key=lambda t: tuple(t.rgb.mean(axis=(0, 1)).astype(int)) + (t.id,))
    ]
    for tile in result.tiles:
        tile.suppressed = tile.solid and len(tile.occurrences) >= blank_frequency
    result.suppressed_count = sum(t.suppressed for t in result.tiles)
    return result
