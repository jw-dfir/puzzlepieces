"""Record-level reconstruction engine with a per-evidence calibrated seam model.

Unit of reconstruction
    A *native cached bitmap record instance* (normally a 64×64 RDP8 tile), rebuilt from the parser's deduplicated
    subtiles. Deduplication remains a storage/search optimisation: every record keeps its own identity and source
    position, so identical pixels stored in different records are never forced into one place, and the pixels of one
    record are never split.

Seam model
    Each seam is described by boundary continuity relative to the tiles' internal gradients, a stricter
    prediction-based continuity, background agreement and text-line (ink-row) alignment. The model is calibrated on
    the evidence itself: *positives* are seams inside the input's own records (each record split in half), *negatives*
    are random half-tiles with the same background colour, which is the hard case of text on a shared terminal or
    window background. The result is a log-likelihood ratio (LLR) per seam. The log-likelihood ratios are heuristic
    (naive-Bayes binning) and are reported as such.

Assembly
    Blocks merge only when the posterior of the merge is high. The posterior combines a prior (cache-order adjacency,
    whose rate and direction are measured per source, or a small visual prior), the sum of LLRs of *all* seams the
    merge would create, and every competing proposal for the same tile edge. One strongly contradicting seam vetoes a
    merge. Uniform records can fill interior holes; such fills are new, labelled instances of a cached tile.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from threading import Event

import numpy as np
from scipy.spatial import cKDTree

from models import Assembly, Link, Placement, Progress, Tile, check_cancel

RIGHT, LEFT, BOTTOM, TOP = 0, 1, 2, 3
SIDES = ((RIGHT, LEFT), (BOTTOM, TOP))  # (side of A, side of B) for axis 0 (B right of A) and axis 1 (B below A)
BAND = 8
INK = 0.25
FLAT = 0.02
LLR_MIN, LLR_MAX = -8.0, 6.0
IMPOSSIBLE = -99.0
FILL_REASON = "background fill (repeated cached tile)"


@dataclass(frozen=True, slots=True)
class EngineSettings:
    confidence: float = 0.95  # minimum posterior probability for accepting a merge
    # Probability that an edge's neighbour is present but not explained by cache order, spread evenly over all
    # matchable records. With thousands of records a single seam can rarely justify a join on its own.
    visual_prior: float = 0.3
    veto: float = 4.0  # a created seam with LLR <= -veto blocks the merge
    candidates: int = 16  # indexed visual candidates per edge
    sequence_window: int = 32  # cache-order neighbours scored per edge
    include_background: bool = False  # match uniform records like any other record
    fill_backgrounds: bool = True  # allow uniform records to fill bounded interior holes
    use_sequence: bool = True
    # After confident assembly, keep joining the best remaining non-contradicted fits. These are guesses and are
    # flagged on every placement, link and region.
    speculative: bool = False
    speculative_min_llr: float = 0.5
    max_rounds: int = 40
    calibration_samples: int = 3000
    workers: int = min(4, os.cpu_count() or 1)

    def validate(self) -> None:
        if not 0.5 <= self.confidence < 1 or not 0 < self.visual_prior <= 1 or self.veto <= 0:
            raise ValueError("Confidence must be within 0.5–1, visual prior within 0–0.5 and veto positive")
        if self.candidates < 2 or self.sequence_window < 0 or self.workers < 1 or self.max_rounds < 1:
            raise ValueError("Invalid candidate count/window/workers/rounds")


def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


# --------------------------------------------------------------------------------------------------------------------
# Records


@dataclass(slots=True)
class Record:
    tile: Tile  # whole native record instance with exactly one occurrence
    chain: str  # sequence domain: explicit group, else source file (or directory for loose BMPs)
    order: int
    flat: bool


def build_records(tiles: list[Tile], cancel: Event | None = None) -> list[Record]:
    """Re-assemble native bitmap records from deduplicated subtiles, restoring every occurrence."""
    groups: dict[tuple, list] = defaultdict(list)
    for tile in tiles:
        check_cancel(cancel)
        for occurrence in tile.occurrences:
            groups[occurrence.source, occurrence.record_offset, occurrence.sequence].append((tile, occurrence))
    records = []
    for parts in groups.values():
        check_cancel(cancel)
        first = parts[0][1]
        width = max(first.native_width, max(o.sub_x + t.width for t, o in parts))
        height = max(first.native_height, max(o.sub_y + t.height for t, o in parts))
        rgb = np.zeros((height, width, 3), np.uint8)
        for tile, o in parts:
            rgb[o.sub_y : o.sub_y + tile.height, o.sub_x : o.sub_x + tile.width] = tile.rgb
        rgb.flags.writeable = False
        data = rgb.tobytes()
        occurrence = replace(first, sub_x=0, sub_y=0, native_width=width, native_height=height)
        identity = hashlib.sha256(
            f"{first.source_sha256}|{first.source}|{first.record_offset}|{first.sequence}|".encode()
            + hashlib.sha256(data).digest()
        ).hexdigest()
        solid = bool(np.max(np.ptp(rgb.reshape(-1, 3).astype(np.int16), axis=0)) <= 2)
        instance = Tile(identity, rgb, hashlib.md5(data, usedforsecurity=False).hexdigest(), [occurrence], solid)
        chain = first.group or (os.path.dirname(first.source) if first.record_offset is None else first.source)
        records.append(Record(instance, chain, first.sequence, solid))
    records.sort(key=lambda r: (r.chain, r.order, r.tile.id))
    return records


# --------------------------------------------------------------------------------------------------------------------
# Edge features and the seam model


class EdgeSet:
    """Padded per-side edge features of a list of RGB arrays."""

    def __init__(self, images: list[np.ndarray], size: int | None = None):
        n = len(images)
        size = size or max((max(i.shape[0], i.shape[1]) for i in images), default=2)
        self.size = size
        self.length = np.zeros((4, n), np.int32)
        self.e0 = np.zeros((4, n, size, 3), np.float32)
        self.e1 = np.zeros((4, n, size, 3), np.float32)
        self.ink = np.zeros((4, n, size), bool)
        self.bg = np.zeros((4, n, 3), np.float32)
        self.flat = np.zeros((4, n), bool)
        self.inked = np.zeros((4, n), np.int32)
        for i, image in enumerate(images):
            rgb = image.astype(np.float32) / 255
            transposed = rgb.transpose(1, 0, 2)
            # Each view is oriented so its last column is the edge facing the neighbour on that side.
            for side, view in enumerate((rgb, rgb[:, ::-1], transposed, transposed[:, ::-1])):
                length = view.shape[0]
                self.length[side, i] = length
                self.e0[side, i, :length] = view[:, -1]
                self.e1[side, i, :length] = view[:, -2] if view.shape[1] > 1 else view[:, -1]
                band = view[:, -min(BAND, view.shape[1]) :]
                bg = np.median(band.reshape(-1, 3), axis=0)
                self.bg[side, i] = bg
                ink = (np.abs(band - bg).max(axis=2) > INK).any(axis=1)
                self.ink[side, i, :length] = ink
                self.inked[side, i] = int(ink.sum())
                self.flat[side, i] = bool(view[:, -1].std(axis=0).max() < FLAT)

    def descriptors(self, side: int) -> np.ndarray:
        n = self.e0.shape[1]
        segments = 8 if self.size % 8 == 0 else 1
        edge = self.e0[side].reshape(n, segments, -1, 3).mean(axis=2).reshape(n, -1)
        ink = self.ink[side].reshape(n, segments, -1).mean(axis=2) * 0.5
        return np.ascontiguousarray(np.concatenate([edge, ink], axis=1))


def seam_features(ea: EdgeSet, a: np.ndarray, sa: int, eb: EdgeSet, b: np.ndarray, sb: int) -> dict[str, np.ndarray]:
    la, lb = ea.length[sa, a], eb.length[sb, b]
    mask = np.arange(ea.size)[None, :] < la[:, None]
    a0, a1, b0, b1 = ea.e0[sa, a], ea.e1[sa, a], eb.e0[sb, b], eb.e1[sb, b]
    step = np.abs(a0 - b0).max(axis=2)
    internal = np.maximum(np.abs(a0 - a1).max(axis=2), np.abs(b0 - b1).max(axis=2))
    predicted = np.minimum(np.abs(2 * a0 - a1 - b0).max(axis=2), np.abs(2 * b0 - b1 - a0).max(axis=2))
    count = np.maximum(la, 1)
    ia, ib = ea.ink[sa, a], eb.ink[sb, b]
    na, nb = ea.inked[sa, a], eb.inked[sb, b]
    dil_a = ia | np.roll(ia, 1, axis=1) | np.roll(ia, -1, axis=1)
    dil_b = ib | np.roll(ib, 1, axis=1) | np.roll(ib, -1, axis=1)
    alignment = ((ia & dil_b).sum(axis=1) + (ib & dil_a).sum(axis=1)) / np.maximum(na + nb, 1)

    # Texture along the edge: smooth gradients and blank gaps continue almost anywhere, so they carry less evidence.
    def along(e):
        return (np.abs(np.diff(e, axis=1)).max(axis=2) * mask[:, 1:]).sum(axis=1) / np.maximum(count - 1, 1)

    return {
        "activity": np.maximum(along(a0), along(b0)),
        "continuity": ((step <= internal + 0.08) & mask).sum(axis=1) / count,
        "strict": ((np.minimum(step, predicted) <= 0.1) & mask).sum(axis=1) / count,
        "background": np.abs(ea.bg[sa, a] - eb.bg[sb, b]).max(axis=1),
        "alignment": np.where((na >= 3) & (nb >= 3), alignment, np.nan),
        "valid": la == lb,
    }


CONTINUITY_BINS = np.array([0.5, 0.75, 0.9, 0.97, 0.999])
ACTIVITY_BINS = np.array([0.004, 0.015, 0.05])
BACKGROUND_BINS = np.array([0.01, 0.03, 0.08, 0.2])
ALIGNMENT_BINS = np.array([0.3, 0.6, 0.85])


def _cells(features: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    continuity = np.digitize(features["continuity"], CONTINUITY_BINS)
    strict = np.digitize(features["strict"], CONTINUITY_BINS)
    background = np.digitize(features["background"], BACKGROUND_BINS)
    alignment = np.where(
        np.isnan(features["alignment"]), 0, np.digitize(np.nan_to_num(features["alignment"]), ALIGNMENT_BINS) + 1
    )
    activity = np.digitize(features["activity"], ACTIVITY_BINS)
    return activity * 36 + continuity * 6 + strict, background * 5 + alignment


class SeamModel:
    """Naive-Bayes seam log-likelihood ratio over two binned feature groups."""

    def __init__(self, continuity_llr: np.ndarray, context_llr: np.ndarray, summary: dict):
        # Within each edge-texture level, better continuity must never lower the evidence: enforce monotonicity.
        table = continuity_llr.reshape(4, 6, 6).copy()
        table = np.maximum.accumulate(np.maximum.accumulate(table, axis=1), axis=2)
        self.continuity_llr = table.reshape(-1)
        self.context_llr = context_llr
        self.summary = summary

    @classmethod
    def fit(cls, positive: dict, negative: dict, label: str) -> "SeamModel":
        pc, px = _cells(positive)
        nc, nx = _cells(negative)

        def hist(cells, size):
            return (np.bincount(cells, minlength=size) + 1.0) / (len(cells) + size)

        continuity = np.log(hist(pc, 144)) - np.log(hist(nc, 144))
        context = np.log(hist(px, 25)) - np.log(hist(nx, 25))
        model = cls(continuity, context, {})
        lp, ln = model.llr(positive), model.llr(negative)
        model.summary = {
            "calibration": label,
            "positives": int(len(pc)),
            "negatives": int(len(nc)),
            "tpr_llr_ge_2": round(float((lp >= 2).mean()), 4),
            "fpr_llr_ge_2": round(float((ln >= 2).mean()), 4),
            "tpr_llr_ge_0": round(float((lp >= 0).mean()), 4),
            "fpr_llr_ge_0": round(float((ln >= 0).mean()), 4),
        }
        return model

    @classmethod
    def fallback(cls) -> "SeamModel":
        # Used only when the evidence contains too few full records to calibrate. Monotone, deliberately cautious.
        t, c, s = np.meshgrid(np.array([0.2, 0.5, 0.8, 1.0]), np.arange(6), np.arange(6), indexing="ij")
        continuity = (t * (-6.0 + 1.0 * c + 1.2 * s)).reshape(-1)
        b, a = np.meshgrid(np.arange(5), np.arange(5), indexing="ij")
        context = (np.array([1.0, 0.5, -1.0, -3.0, -5.0])[b] + np.array([0.0, -2.0, -0.5, 0.5, 1.0])[a]).reshape(-1)
        model = cls(continuity, context, {"calibration": "uncalibrated fallback"})
        model.summary.update(tpr_llr_ge_2=None, fpr_llr_ge_2=None)
        return model

    def llr(self, features: dict[str, np.ndarray]) -> np.ndarray:
        continuity, context = _cells(features)
        value = np.clip(self.continuity_llr[continuity] + self.context_llr[context], LLR_MIN, LLR_MAX)
        return np.where(features["valid"], value, IMPOSSIBLE)


def calibrate(records: list[Record], grid: int, axis: int, samples: int, seed: int = 0) -> SeamModel:
    """Fit the seam model on seams inside the evidence's own records versus same-background random pairs."""
    rng = np.random.default_rng(seed + axis)
    usable = [r.tile.rgb for r in records if not r.flat and r.tile.width == r.tile.height == grid and grid >= 16]
    if len(usable) < 100:
        return SeamModel.fallback()
    if len(usable) > samples:
        usable = [usable[i] for i in sorted(rng.choice(len(usable), samples, replace=False))]
    sa, sb = SIDES[axis]
    positives, negatives = [], []
    # Cut every record at three positions; each cut is a genuine seam with real content on both sides.
    for cut in sorted({grid // 4, grid // 2, 3 * grid // 4}):
        if axis == 0:
            first, second = [u[:, :cut] for u in usable], [u[:, cut:] for u in usable]
        else:
            first, second = [u[:cut] for u in usable], [u[cut:] for u in usable]
        ef, es = EdgeSet(first, grid), EdgeSet(second, grid)
        n = len(usable)
        index = np.arange(n)
        positives.append(seam_features(ef, index, sa, es, index, sb))
        # Hard negatives: another record's piece with the same background (a shared terminal/window background).
        partner = np.full(n, -1)
        for i in range(n):
            options = rng.choice(n, min(n, 48), replace=False)
            options = options[
                (options != i)
                & (np.abs(es.bg[sb, options] - ef.bg[sa, i]).max(axis=1) < 0.03)
                & np.any(es.e0[sb, options] != es.e0[sb, i], axis=(1, 2))
            ]
            if len(options):
                partner[i] = options[0]
        chosen = partner >= 0
        if chosen.sum() < 50:
            partner = (index + 1 + rng.integers(0, n - 1, n)) % n
            chosen = np.ones(n, bool)
        negatives.append(seam_features(ef, index[chosen], sa, es, partner[chosen], sb))

    def join(parts):
        return {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}

    positive, negative = join(positives), join(negatives)
    n = len(usable)
    return SeamModel.fit(
        positive,
        negative,
        f"{len(positive['valid'])} internal seams in {n} records of this evidence, axis {'horizontal' if axis == 0 else 'vertical'}",
    )


class Scorer:
    def __init__(self, edges: EdgeSet, models: tuple[SeamModel, SeamModel]):
        self.edges = edges
        self.models = models
        self.cache: dict[tuple[int, int, int], float] = {}

    def many(self, pairs: list[tuple[int, int, int]]) -> list[float]:
        missing = [p for p in dict.fromkeys(pairs) if p not in self.cache]
        for axis in (0, 1):
            subset = [p for p in missing if p[2] == axis]
            sa, sb = SIDES[axis]
            for start in range(0, len(subset), 20000):
                chunk = subset[start : start + 20000]
                a = np.fromiter((p[0] for p in chunk), np.intp, len(chunk))
                b = np.fromiter((p[1] for p in chunk), np.intp, len(chunk))
                llr = self.models[axis].llr(seam_features(self.edges, a, sa, self.edges, b, sb))
                self.cache.update(zip(chunk, llr.tolist()))
        return [self.cache[p] for p in pairs]


# --------------------------------------------------------------------------------------------------------------------
# Cache-order prior


def sequence_priors(
    by_chain, scorer: Scorer, window: int, iterations: int = 60
) -> dict[str, dict[tuple[int, int], float]]:
    """Estimate, per chain, the probability that record k+d is the right (axis 0) / lower (axis 1) neighbour of k.

    An EM mixture over offsets: each record has at most one such neighbour among the offsets, or none. Offsets compete
    for the same seam evidence, so nearby-but-not-adjacent records (which share a window or wallpaper and therefore
    look alike) do not receive the credit of the true neighbour. Offsets whose estimated weight stays below 2% are
    discarded. Cache order thus informs assembly only to the extent it is supported by this evidence.
    """
    priors: dict[str, dict[tuple[int, int], float]] = {}
    for chain, members in by_chain.items():
        found: dict[tuple[int, int], float] = {}
        priors[chain] = found
        span = min(window, len(members) // 4)
        if span < 2:
            continue
        offsets = [d for d in range(-span, span + 1) if d]
        n = len(members)
        for axis in (0, 1):
            ratio = np.zeros((n, len(offsets)))
            for column, d in enumerate(offsets):
                rows = range(max(0, -d), n - max(0, d))
                values = scorer.many([(members[k], members[k + d], axis) for k in rows])
                ratio[list(rows), column] = np.exp(np.clip(values, LLR_MIN, LLR_MAX))
            weights = np.full(len(offsets), 0.5 / len(offsets))
            for _ in range(iterations):
                joint = ratio * weights
                responsibility = joint / (max(1e-6, 1 - weights.sum()) + joint.sum(axis=1, keepdims=True))
                weights = responsibility.mean(axis=0)
            for d, weight in zip(offsets, weights):
                if weight >= 0.02:
                    found[axis, d] = round(float(weight), 4)
    return priors


# --------------------------------------------------------------------------------------------------------------------
# Candidates and block assembly


@dataclass(slots=True)
class Candidate:
    a: int
    b: int
    axis: int
    llr: float
    prior: float
    sequence: bool


def _candidates(records, matchable, edges, scorer, settings, priors, visual, cancel, progress) -> list[Candidate]:
    index = np.array(matchable, np.intp)
    if len(index) < 2:
        return []
    by_chain = defaultdict(list)
    for i in matchable:
        by_chain[records[i].chain].append(i)
    position = {i: (chain, k) for chain, members in by_chain.items() for k, i in enumerate(members)}
    pairs: set[tuple[int, int, int]] = set()
    k = min(len(index), settings.candidates + 1)
    for axis, (sa, sb) in enumerate(SIDES):
        tree = cKDTree(edges.descriptors(sb)[index])
        query = edges.descriptors(sa)[index]
        for start in range(0, len(index), 1024):
            check_cancel(cancel)
            _, found = tree.query(query[start : start + 1024], k=k, workers=settings.workers)
            found = found.reshape(len(query[start : start + 1024]), -1)
            for row, i in enumerate(index[start : start + 1024].tolist()):
                for j in index[found[row][found[row] < len(index)]].tolist():
                    if j != i:
                        pairs.add((i, j, axis))
            if progress:
                progress("Indexing edge candidates", axis * len(index) + start, 2 * len(index))
        window = settings.sequence_window
        for members in by_chain.values():
            for k0, i in enumerate(members):
                for j in members[max(0, k0 - window) : k0 + window + 1]:
                    if j != i:
                        pairs.add((i, j, axis))
    ordered = sorted(pairs)
    candidates = []
    for start in range(0, len(ordered), 50000):
        check_cancel(cancel)
        chunk = ordered[start : start + 50000]
        for (a, b, axis), llr in zip(chunk, scorer.many(chunk)):
            if llr <= 0:
                continue
            ca, cb = position[a], position[b]
            step = cb[1] - ca[1] if ca[0] == cb[0] else 0
            prior = max(visual, priors.get(records[a].chain, {}).get((axis, step), 0.0))
            sequence = prior > visual
            candidates.append(Candidate(a, b, axis, llr, prior, sequence))
        if progress:
            progress("Scoring seams", start + len(chunk), len(ordered))
    return candidates


def combined(values: list[float]) -> float:
    """Combine seam LLRs of one merge. Seams along one join are correlated (shared background, repeated UI), so
    positive evidence beyond four seams is tempered by sqrt(4/n); contradicting evidence always counts in full."""
    positive = sum(v for v in values if v > 0)
    negative = sum(v for v in values if v < 0)
    count = sum(1 for v in values if v > 0)
    return negative + (positive * math.sqrt(4 / count) if count > 4 else positive)


class Blocks:
    def __init__(self, n: int):
        self.block = list(range(n))
        self.pos = [(0, 0)] * n
        self.cells: dict[int, dict[tuple[int, int], int]] = {i: {(0, 0): i} for i in range(n)}

    def merge(self, keep: int, other: int, shift: tuple[int, int]) -> None:
        for (x, y), r in self.cells.pop(other).items():
            cell = (x + shift[0], y + shift[1])
            self.cells[keep][cell] = r
            self.block[r] = keep
            self.pos[r] = cell

    def contacts(self, keep: int, other: int, shift: tuple[int, int]) -> list[tuple[int, int, int]] | None:
        """Seams (a, b, axis) the merge would create, or None if the blocks overlap."""
        target = self.cells[keep]
        seams = []
        for (x, y), r in self.cells[other].items():
            cell = (x + shift[0], y + shift[1])
            if cell in target:
                return None
            for dx, dy, axis in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
                neighbour = target.get((cell[0] + dx, cell[1] + dy))
                if neighbour is not None:
                    seams.append((r, neighbour, axis) if dx + dy > 0 else (neighbour, r, axis))
        return seams


def stitch(
    tiles: list[Tile],
    settings: EngineSettings | None = None,
    *,
    fixed: dict[str, Placement] | None = None,
    cancel: Event | None = None,
    progress: Progress | None = None,
) -> Assembly:
    """Reconstruct regions from deduplicated tiles carrying full occurrence metadata.

    Pass *all* tiles (including suppressed backgrounds) so native records are complete. Placements refer to the
    record-level instance tiles listed in ``Assembly.instances``.
    """
    started = time.perf_counter()
    settings = settings or EngineSettings()
    settings.validate()
    records = build_records(tiles, cancel)
    n = len(records)
    result = Assembly(settings={**asdict(settings), "layout_kind": "inferred", "unit": "native record instance"})
    result.instances = [r.tile for r in records]
    known = {r.tile.id for r in records}
    result.placements.update({k: p for k, p in (fixed or {}).items() if k in known})
    if not n:
        return result
    dims = Counter((r.tile.width, r.tile.height) for r in records)
    grid = max(dims.items(), key=lambda item: (item[1], item[0][0]))[0][0]
    pinned = {i for i, r in enumerate(records) if r.tile.id in result.placements}
    matchable = [
        i
        for i, r in enumerate(records)
        if i not in pinned
        and r.tile.width <= grid
        and r.tile.height <= grid
        and (settings.include_background or not r.flat)
    ]
    if progress:
        progress("Calibrating seam model on this evidence", 0, 3)
    models = tuple(calibrate(records, grid, axis, settings.calibration_samples) for axis in (0, 1))
    if progress:
        progress("Extracting edge features", 1, 3)
    edges = EdgeSet([r.tile.rgb for r in records])
    scorer = Scorer(edges, models)
    by_chain = defaultdict(list)
    for i in matchable:
        by_chain[records[i].chain].append(i)
    visual = settings.visual_prior / max(1, len(matchable))
    if progress:
        progress("Measuring cache-order structure", 2, 3)
    window = settings.sequence_window if settings.use_sequence else 0
    priors = sequence_priors(by_chain, scorer, window) if window else {}
    candidates = _candidates(records, matchable, edges, scorer, settings, priors, visual, cancel, progress)
    blocks = Blocks(n)
    links: list[Link] = []
    incident: dict[int, float] = {}
    evidence: Counter = Counter()
    vetoed = 0

    def full(i: int, axis: int) -> bool:
        # Only full-size cells may have a neighbour on that side; partial edge records end a row/column.
        return (records[i].tile.width if axis == 0 else records[i].tile.height) == grid

    speculative: set[int] = set()
    rounds = [("confident", r) for r in range(settings.max_rounds)]
    if settings.speculative:
        rounds += [("speculative", r) for r in range(settings.max_rounds * 4)]
    done_stage = None
    for stage, round_number in rounds:
        if stage == done_stage:
            continue
        check_cancel(cancel)
        proposals: dict[tuple[int, int, tuple[int, int]], list[Candidate]] = defaultdict(list)
        candidates = [c for c in candidates if blocks.block[c.a] != blocks.block[c.b]]
        for c in candidates:
            ba, bb = blocks.block[c.a], blocks.block[c.b]
            if ba == bb or not full(c.a, c.axis):
                continue
            pa, pb = blocks.pos[c.a], blocks.pos[c.b]
            shift = (pa[0] + (c.axis == 0) - pb[0], pa[1] + (c.axis == 1) - pb[1])
            if (len(blocks.cells[ba]), -ba) < (len(blocks.cells[bb]), -bb):
                ba, bb, shift = bb, ba, (-shift[0], -shift[1])
            proposals[ba, bb, shift].append(c)
        scored = []
        for (keep, other, shift), seeds in proposals.items():
            seams = blocks.contacts(keep, other, shift)
            if not seams or any(not full(a, axis) for a, _, axis in seams):
                continue
            values = scorer.many(seams)
            if min(values) <= -settings.veto:
                vetoed += 1
                continue
            prior = max(c.prior for c in seeds)
            log_odds = logit(prior) + combined(values)
            sides = {(c.a, SIDES[c.axis][0]) for c in seeds} | {(c.b, SIDES[c.axis][1]) for c in seeds}
            sides.add(("blocks", keep, other))  # alternative relative placements of one block pair are exclusive
            scored.append([log_odds, keep, other, shift, seams, values, seeds, sides])
        # Competing proposals for the same tile edge, or for the same pair of blocks at a different offset, are mutually
        # exclusive: normalise against all of them plus "none".
        by_side: dict[tuple[int, int], list[float]] = defaultdict(list)
        for item in scored:
            for side in item[7]:
                by_side[side].append(item[0])
        normaliser = {}
        for side, rivals in by_side.items():
            top = max(max(rivals), 0.0)
            normaliser[side] = (top, math.exp(-top) + sum(math.exp(v - top) for v in rivals))
        evaluated = []
        for log_odds, keep, other, shift, seams, values, seeds, sides in scored:
            posterior = 1.0
            for side in sides:
                top, denominator = normaliser[side]
                posterior = min(posterior, math.exp(log_odds - top) / denominator)
            if stage == "confident" and posterior >= settings.confidence:
                evaluated.append((posterior, keep, other, shift, seams, values, seeds))
            elif stage == "speculative" and combined(values) >= settings.speculative_min_llr:
                evaluated.append((posterior, keep, other, shift, seams, values, seeds))
        if stage == "speculative":
            # No confident option is left: take the strongest remaining evidence first.
            evaluated.sort(key=lambda item: (-combined(item[5]), item[1], item[2], item[3]))
        else:
            evaluated.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
        touched: set[int] = set()
        merged = 0
        for posterior, keep, other, shift, seams, values, seeds in evaluated:
            if keep in touched or other in touched:
                continue  # re-evaluated next round against the updated geometry
            blocks.merge(keep, other, shift)
            touched.update((keep, other))
            merged += 1
            sequence = any(c.sequence for c in seeds)
            kind = "cache order + seams" if sequence else "seams only"
            if stage == "speculative":
                kind = "SPECULATIVE guess, " + kind
                speculative.update(r for seam in seams for r in seam[:2])
            evidence[kind] += 1
            reason = f"{kind}; {len(seams)} seam(s), combined LLR {combined(values):+.1f}"
            for (a, b, axis), value in zip(seams, values):
                links.append(
                    Link(
                        records[a].tile.id,
                        records[b].tile.id,
                        grid if axis == 0 else 0,
                        grid if axis == 1 else 0,
                        round(posterior, 4),
                        round(value, 3),
                        reason,
                    )
                )
                incident[a] = min(incident.get(a, 1.0), posterior)
                incident[b] = min(incident.get(b, 1.0), posterior)
        if progress:
            progress("Assembling regions", round_number + 1, settings.max_rounds)
        if not merged:
            done_stage = stage

    fills = _fill_holes(records, blocks, scorer, settings, grid, pinned, cancel) if settings.fill_backgrounds else []
    fill_cells = defaultdict(list)
    for instance, block, cell, confidence in fills:
        result.instances.append(instance)
        fill_cells[block].append((instance, cell, confidence))
        evidence[FILL_REASON] += 1

    def extent(b):
        cells = list(blocks.cells[b]) + [cell for _, cell, _ in fill_cells[b]]
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1

    regions = [b for b, cells in blocks.cells.items() if len(cells) >= 2]
    regions.sort(key=lambda b: (-len(blocks.cells[b]), -extent(b)[3], min(blocks.cells[b].values())))
    total_area = sum((extent(b)[2] + 1) * (extent(b)[3] + 1) for b in regions)
    width_limit = max(8, math.ceil(math.sqrt(total_area * 1.6)))
    origin_x = math.ceil(max(p.x for p in result.placements.values()) / grid) + 4 if result.placements else 0
    x, y, shelf = origin_x, 0, 0
    summaries = []
    for cluster, b in enumerate(regions, 1):
        check_cancel(cancel)
        min_x, min_y, width, height = extent(b)
        if x > origin_x and x + width > origin_x + width_limit:
            x, y, shelf = origin_x, y + shelf + 1, 0
        confidences = []
        for (cx, cy), r in blocks.cells[b].items():
            tile = records[r].tile
            confidences.append(incident.get(r, 0.0))
            result.placements[tile.id] = Placement(
                tile.id,
                (x + cx - min_x) * grid,
                (y + cy - min_y) * grid,
                incident.get(r),
                cluster,
                speculative=r in speculative,
            )
        for instance, (cx, cy), confidence in fill_cells[b]:
            result.placements[instance.id] = Placement(
                instance.id, (x + cx - min_x) * grid, (y + cy - min_y) * grid, confidence, cluster
            )
            result.links.append(Link(instance.id, instance.md5, 0, 0, confidence, 0.0, FILL_REASON))
        orders = [records[r].order for r in blocks.cells[b].values()]
        summaries.append(
            {
                "region": cluster,
                "records": len(blocks.cells[b]),
                "background_fills": len(fill_cells[b]),
                "cells": [width, height],
                "pixels": [width * grid, height * grid],
                "fill_ratio": round((len(blocks.cells[b]) + len(fill_cells[b])) / (width * height), 3),
                "speculative_records": sum(1 for r in blocks.cells[b].values() if r in speculative),
                "min_confidence": round(min(confidences), 4),
                "mean_confidence": round(sum(confidences) / len(confidences), 4),
                "sources": sorted({records[r].tile.occurrences[0].source for r in blocks.cells[b].values()}),
                "sequence_span": [min(orders), max(orders)],
            }
        )
        x += width + 1
        shelf = max(shelf, height)
    result.links[:0] = links
    result.unplaced = [r.tile.id for r in records if r.tile.id not in result.placements]
    result.settings.update(
        {
            "grid": grid,
            "records": n,
            "matchable_records": len(matchable),
            "seam_models": [m.summary for m in models],
            "cache_order_priors": {
                chain: [
                    {"axis": "right" if axis == 0 else "below", "offset": d, "rate": rate}
                    for (axis, d), rate in found.items()
                ]
                for chain, found in priors.items()
            },
            "candidates_considered": len(candidates),
            "merges_vetoed": vetoed,
            "evidence_counts": dict(evidence),
            "regions": summaries,
        }
    )
    result.elapsed_seconds = time.perf_counter() - started
    if progress:
        progress("Assembly complete", len(result.placements), n)
    return result


def _fill_holes(records, blocks: Blocks, scorer: Scorer, settings: EngineSettings, grid: int, pinned, cancel):
    """Fill interior holes with uniform cached records that match neighbours on opposite sides.

    A cache stores one copy of a repeated background tile while the screen can show it many times, so a fill creates a
    new *instance* of the record. It is made only where one colour is clearly better than every other.
    """
    colours = {}
    for i, r in enumerate(records):
        if r.flat and i not in pinned and r.tile.width == r.tile.height == grid:
            colours.setdefault(r.tile.rgb[0, 0].tobytes(), i)
    flats = list(colours.values())
    fills = []
    if not flats:
        return fills
    for _ in range(3):
        added = 0
        for b, cells in list(blocks.cells.items()):
            check_cancel(cancel)
            if len(cells) < 2:
                continue
            occupied = dict(cells)
            occupied.update({cell: source for _, block, cell, _, source in fills if block == b})
            xs, ys = [c[0] for c in cells], [c[1] for c in cells]
            for cx in range(min(xs), max(xs) + 1):
                for cy in range(min(ys), max(ys) + 1):
                    if (cx, cy) in occupied:
                        continue
                    around = {d: occupied.get((cx + d[0], cy + d[1])) for d in ((-1, 0), (1, 0), (0, -1), (0, 1))}
                    if not (
                        (around[-1, 0] is not None and around[1, 0] is not None)
                        or (around[0, -1] is not None and around[0, 1] is not None)
                    ):
                        continue
                    choices = []
                    for f in flats:
                        seams = [
                            (other, f, 0 if dy == 0 else 1) if dx + dy < 0 else (f, other, 0 if dy == 0 else 1)
                            for (dx, dy), other in around.items()
                            if other is not None
                        ]
                        values = scorer.many(seams)
                        if min(values) >= 1.0:
                            choices.append((sum(values), f))
                    choices.sort(reverse=True)
                    if not choices or (len(choices) > 1 and choices[0][0] - choices[1][0] < 2.0):
                        continue
                    total, f = choices[0]
                    source = records[f].tile
                    identity = hashlib.sha256(f"{source.id}|fill|{b}|{cx}|{cy}".encode()).hexdigest()
                    instance = Tile(identity, source.rgb, source.md5, list(source.occurrences), solid=True)
                    confidence = round(1 / (1 + math.exp(-total)), 4)
                    fills.append((instance, b, (cx, cy), confidence, f))
                    occupied[cx, cy] = f
                    added += 1
        if not added:
            break
    return [(instance, b, cell, confidence) for instance, b, cell, confidence, _ in fills]
