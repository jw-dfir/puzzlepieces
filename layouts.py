from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import replace
from threading import Event

from models import Assembly, IngestResult, Placement, Tile, check_cancel


def contact_sheet(tiles: list[Tile], cancel: Event | None = None) -> Assembly:
    columns = max(1, math.ceil(math.sqrt(len(tiles) * 1.5)))
    result = Assembly(settings={"layout_kind": "contact_sheet", "columns": columns})
    for index, tile in enumerate(tiles):
        check_cancel(cancel)
        result.placements[tile.id] = Placement(tile.id, index % columns * 32, index // columns * 32, None, None)
    return result


def native_bitmaps(data: IngestResult, cancel: Event | None = None) -> tuple[list[Tile], Assembly]:
    groups = defaultdict(list)
    for tile in data.tiles:
        for occurrence in tile.occurrences:
            check_cancel(cancel)
            groups[occurrence.source, occurrence.record_offset, occurrence.sequence].append((tile, occurrence))
    total_area = sum(
        max(o.native_width for _, o in values) * max(o.native_height for _, o in values) for values in groups.values()
    )
    target_width = max(64, math.ceil(math.sqrt(total_area * 1.5) / 32) * 32)
    instances = []
    result = Assembly(settings={"layout_kind": "native_bitmaps", "duplicates_restored": True})
    x = y = shelf_height = 0
    for cluster, (key, values) in enumerate(sorted(groups.items(), key=lambda item: (item[0][0], item[0][2])), 1):
        check_cancel(cancel)
        width = math.ceil(max(o.native_width for _, o in values) / 32) * 32
        height = math.ceil(max(o.native_height for _, o in values) / 32) * 32
        if x and x + width > target_width:
            x, y, shelf_height = 0, y + shelf_height + 32, 0
        for tile, occurrence in values:
            identity = hashlib.sha256(f"{tile.id}|{key}|{occurrence.sub_x}|{occurrence.sub_y}".encode()).hexdigest()
            instance = replace(tile, id=identity, occurrences=[occurrence], suppressed=False)
            instances.append(instance)
            result.placements[identity] = Placement(identity, x + occurrence.sub_x, y + occurrence.sub_y, 1.0, cluster)
        x += width + 32
        shelf_height = max(shelf_height, height)
    return instances, result
