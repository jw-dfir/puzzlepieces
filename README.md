# Puzzle Pieces — RDP Bitmap Cache Stitcher

Python 3.11+ / PyQt6 desktop application for automatic RDP bitmap cache reconstruction. Drop a directory or `.bin` / `.bmc` file onto the canvas. The application decodes, hashes, filters, deduplicates and attempts a conservative assembly automatically. Unsupported or ambiguous material remains available for review.

**[Download the Windows build](../../releases/latest)** — a single `.exe`, no install or Python required.

## Screenshots

All screenshots below are rendered from the bundled synthetic test fixture (`synthetic.py`), not real case data.

**Inferred reconstruction** — joined regions from a synthetic RDP8 cache, each one a cluster of native cached records the engine is confident about:

![Inferred reconstruction overview](docs/screenshots/overview.png)

**A reconstructed region with its evidence** — clicking a tile shows its provenance and the seam evidence that joined it:

![Region preview and tile evidence](docs/screenshots/region-and-evidence.png)

**Speculative full assembly** — after confident joins run out, it keeps joining the best remaining fits to build the largest image it can; guessed tiles are outlined in orange:

![Speculative full assembly with guessed tiles outlined in orange](docs/screenshots/speculative-mode.png)

## Getting started

Download the [Windows executable](../../releases/latest) and launch it. No Python installation is required.

1. Drop a cache file or folder onto the canvas.
2. Review the reconstructed regions and unplaced tiles.
3. Adjust placements manually where needed.
4. Export the relevant region or canvas as a PNG with its forensic report.

### Run from source

Requires Python 3.11 or later. Download or clone the repository, then run these commands from its folder.

**Windows PowerShell:**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe gui.py
```

**Linux / macOS:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gui.py
```

For batch processing, use the CLI with your environment’s Python:

```bash
python cli.py /path/to/cache-or-tiles -o /path/to/output
```

## Supported inputs

| Input | Supported formats |
| --- | --- |
| RDP bitmap caches | `Cache*.bin` with the `RDP8bmp` signature, versions 1 and 6. |
| Legacy bitmap caches | `bcache2.bmc`, `bcache22.bmc`, `bcache23.bmc` and `bcache24.bmc`. |
| Extracted tiles | Loose BMP files, including tiles extracted with bmc-tools. |

Preserve original BMC filenames to help identify the correct format. Unsupported or malformed records generate warnings.

## Reconstruction

Puzzle Pieces compares neighbouring pixel edges and measured cache-order patterns to assemble cached records into regions. Records without a sufficiently confident match remain available for manual review. Source files are read without modification.

Expect partial windows, readable text runs, toolbars and other fragments. A complete desktop may not be recoverable, particularly where records are missing or many tiles share similar backgrounds.

**Reconstructed regions are inferred arrangements.** Their placement does not establish original screen coordinates, capture time or session boundaries. Confidence scores are heuristic model estimates, not validated probabilities of correctness.

Speculative assembly continues with weaker matches when confident joins run out. Guessed tiles are outlined in orange and require careful review.

## Workspace controls

| Action | Control |
| --- | --- |
| Import files or folders | Toolbar or drag onto the canvas. Each import replaces the current workspace. |
| Pan | Middle-drag or Space + left-drag. |
| Zoom | Ctrl + mouse wheel. |
| Fit tiles to view | `F` or toolbar. |
| Select multiple tiles | Drag a selection rectangle. |
| Move tiles | Drag selected tiles; placements snap to the grid. |
| Place an unplaced tile | Drag from the sidebar or double-click. |
| Pin a correction | Manual placement or movement pins the tile automatically. |
| Release a pin | Select tiles, then **Unpin selected corrections**. |
| Return tiles to the sidebar | Select tiles and press `Delete`. |
| Undo a manual edit | `Ctrl+Z`. |
| Draw an export crop | Enable **Draw crop**, then drag. Press `Esc` to clear it. |
| View a region | Click its preview under **Reconstructed regions**. |
| Export a region | Select its preview, then **Export selected region PNG**. |

Pinned corrections retain their coordinates during reassembly. Automatic reassembly clears the manual undo history.

## Export and audit

Export the canvas, a crop, selected tiles or individual regions as PNG files. Missing areas remain transparent, and interface overlays are excluded.

Exports include SHA-256 hashes and a forensic JSON report recording source details, record offsets, placements, matching evidence, settings and warnings.

Audit logs are saved automatically to `~/RDP-Stitcher-Audits`. Their hash chain can detect alterations or interior deletions, but cannot prevent complete rewriting or trailing truncation. Preserve a separately trusted copy or hash when using the output as evidence.

## Testing

Install the development dependencies and run the test suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

Tests cover parsing, reconstruction, manual corrections, exports and audit integrity. Synthetic test results do not establish accuracy on every real-world cache.

## Attribution and licensing

Cache-layout research and the legacy palette are based on [ANSSI bmc-tools](https://github.com/ANSSI-FR/bmc-tools). Bitmap decompression follows [Microsoft’s RDP specification](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/b6a3f5c2-0804-4c10-9d25-a321720fd23e).

The source is distributed under CeCILL 2.1. See [LICENSE.txt](LICENSE.txt) and [NOTICE.txt](NOTICE.txt). Dependencies retain their respective licences, including PyQt6’s GPL/commercial terms.

No external extraction tool, model download or runtime network connection is required.
