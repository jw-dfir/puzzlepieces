# 1.2.1 — Puzzle Pieces

- Renamed the application **Puzzle Pieces**, with a puzzle-piece icon (`puzzle.png` / `puzzle.ico`), a console-free launcher (`Puzzle Pieces.pyw`) and a taskbar identity on Windows.
- **Speculative full assembly** (GUI checkbox, `--speculative` on the CLI). After confident joins run out, it keeps joining the best remaining non-contradicted fits (combined seam LLR ≥ 0.5) to build the largest image possible. Guessed tiles are outlined in orange and flagged in placements, links and region summaries (`speculative_records`). On synthetic screens it built one image of 357 tiles at 82.5% join precision, against 102 tiles at 97.5% without it. On the supplied real cache it joined 6,399 records into a 10,560 × 10,560 image, nearly all by guesses: a joined collage, not a recovered desktop.
- **Manual joins**: clicking a region preview selects the whole region, so it can be dragged as one piece against another region. Moved tiles are pinned.

# 1.2.0 — record-level reconstruction with a calibrated seam model

Version 1.1 fixed exports, but reconstruction still produced a collage. Diagnosis on the supplied real cache (`Cache0001.bin`, 6,412 RDP8 64×64 records):

- Matching ran on deduplicated 32×32 subtiles. That split native records and merged spatial instances, and identical subtiles carried conflicting native-geometry links.
- One-pixel similarity cannot place terminal text. Seams usually fall between 8-pixel character cells, so edge columns are background. Within one terminal, the old continuity test accepted about 37% of wrong pairs. The flat-border rule discarded most genuine joins, and the reciprocal-best rule picked among a median of 14 near-equal rivals.
- Cache order carries real adjacency, which was unused as such: in the evidence, record *k−1* is the right-hand neighbour about 44% of the time.

Changes:

- **Native record instances** are the unit of reconstruction. Every occurrence is rebuilt from its subtiles, including suppressed backgrounds. Deduplication remains a storage/search optimisation only.
- **Seam model calibrated per evidence**: genuine internal seams (each record cut at three positions per axis) versus same-background pieces of other records. It uses continuity, prediction continuity, edge texture, background agreement and text-line alignment; flat edges are scored, not rejected. Calibration TPR/FPR is reported in the GUI, report and audit.
- **Cache-order structure measured by EM** over record offsets, per source and per axis. It is used only where supported (≥2%); offsets compete, so nearby similar records do not borrow the true neighbour's credit.
- **Posterior assembly**: prior, plus the combined LLR of every seam a merge creates, normalised against all competing proposals for the same tile edge and for the same block pair at other offsets. A single strongly contradicting seam vetoes a merge, and correlated positive evidence is tempered. Default join confidence is 0.95.
- **Background fills**: uniform cached tiles can fill holes bounded on opposite sides when one colour clearly fits. Each fill is a labelled new instance of the record.
- **Outputs**: region previews with one-click region export. Canvas views and exports are labelled *Inferred reconstruction*, *Contact sheet: not a reconstruction* and *Native cached bitmaps: not a reconstruction*. The CLI writes one PNG per region. Reports include record instances, region summaries, seam-model calibration and cache-order priors.
- **Evaluation**: `synthetic.py` renders Windows-like screens and writes RDP8 caches with cache hits, newest-first order, a scrolled/moved second state and optional eviction. `evaluate.py` measures correct and false joins, recall and coherent regions. `benchmark.py` now uses this workload.
- GUI: drag collision checks use tile rectangles (64-px records on a 32-px snap grid), and tile properties show join evidence.

Settings changed: `threshold`/`margin` are replaced by `confidence` and `visual_prior`. Inputs passed to `engine.stitch` should now be *all* ingested tiles, and the result's placements refer to `Assembly.instances`.

# 1.1.0 — fix fragmented canvas and failed large exports

The first release's separated fragment layout could create an excessively large bounding rectangle and fail PNG export. Its synthetic edge test did not establish reconstruction accuracy on real desktop caches.

- Canvas view selector: **Inferred reconstruction**, **All tiles — contact sheet**, and **Original cached bitmap blocks**.
- Native bitmap view restores repeated subtiles and filtered backgrounds into their known positions within each cache record. It reconstructs the stored bitmap's original pixels, not undocumented desktop coordinates.
- All-unique-tiles export includes representatives that are still unplaced, plus filtered backgrounds. It is a tightly packed tile index explicitly labelled as a contact sheet.
- When full-canvas export has no placed tiles, it exports an explicitly labelled contact sheet instead of silently returning.
- Inferred blocks use tighter, height-aware packing with one-cell separators rather than four-cell separators.
- PNG export above 100 million pixels uses scanline streaming. It preserves native resolution and transparent gaps without allocating the whole canvas. A one-billion-pixel / 250,000-pixel-per-dimension limit remains to bound extreme operations.
- PNG metadata/report exports identify the layout kind and export-specific placements. Native instance records retain source offsets and pixel hashes.

## Using the correction

Close the old application. Replace the extracted `rdp_stitcher` directory with this version and run `py -3.12 gui.py`. Existing dependency installations can be reused.

For readable cached blocks, select **Canvas view → Original cached bitmap blocks**, then export the full canvas or the chosen block. For an image containing every unique tile, select **Area → All unique tiles — contact sheet**, then export.

No cache-only algorithm can promise a faithful original desktop: cache files lack authoritative screen positions, can include different screens/windows, and can omit or overwrite content. A contact sheet is an index; native bitmap geometry is evidence; cross-record adjacency remains inferred.
