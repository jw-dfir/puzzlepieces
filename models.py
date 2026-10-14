from __future__ import annotations

from dataclasses import asdict, dataclass, field
from threading import Event
from typing import Callable

import numpy as np

Progress = Callable[[str, int, int], None]


class Cancelled(Exception):
    pass


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Operation cancelled")


@dataclass(frozen=True, slots=True)
class Occurrence:
    source: str
    source_sha256: str
    file_md5: str
    sequence: int
    mtime_ns: int
    timestamp: float | None = None
    group: str | None = None
    record_offset: int | None = None
    cache_key: str | None = None
    native_width: int = 32
    native_height: int = 32
    sub_x: int = 0
    sub_y: int = 0


@dataclass(slots=True)
class Tile:
    id: str
    rgb: np.ndarray
    md5: str
    occurrences: list[Occurrence]
    solid: bool = False
    suppressed: bool = False

    @property
    def width(self) -> int:
        return self.rgb.shape[1]

    @property
    def height(self) -> int:
        return self.rgb.shape[0]


@dataclass(slots=True)
class IngestResult:
    tiles: list[Tile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    raw_count: int = 0
    duplicate_count: int = 0
    suppressed_count: int = 0
    colour_order: list[str] = field(default_factory=list)

    def active(self, include_background: bool = False) -> list[Tile]:
        return [t for t in self.tiles if include_background or not t.suppressed]


@dataclass(frozen=True, slots=True)
class Link:
    a: str
    b: str
    dx: int
    dy: int
    score: float
    margin: float
    reason: str = "edge + metadata"


@dataclass(frozen=True, slots=True)
class Placement:
    tile_id: str
    x: int
    y: int
    confidence: float | None
    cluster: int | None
    manual: bool = False
    speculative: bool = False  # placed by a speculative (guessed) join


@dataclass(slots=True)
class Assembly:
    placements: dict[str, Placement] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    # Tiles that placements refer to when they are not ingested representatives (record instances, fills).
    instances: list[Tile] = field(default_factory=list)


def tile_record(tile: Tile) -> dict:
    return {
        "id": tile.id,
        "pixel_md5": tile.md5,
        "width": tile.width,
        "height": tile.height,
        "solid": tile.solid,
        "suppressed": tile.suppressed,
        "occurrences": [asdict(o) for o in tile.occurrences],
    }
