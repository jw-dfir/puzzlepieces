## Version 1.2 — reconstruction quality

Verified 13 October 2026 on Windows 11, Python 3.12.10 (PyQt6, NumPy, SciPy, Pillow 12.3).

- `python -m pytest -q tests`: **30 passed**. The four v1.1 engine tests built on identical-border puzzles were replaced by realistic-fixture tests. Windows prints "fatal exception: access violation" stack dumps from pytest's fault handler when worker processes are spawned; the unmodified v1.1 suite prints the same dumps, and all tests pass.
- `ruff check .` and `ruff format --check .`: passed. `python -m compileall`: passed.

### Synthetic ground truth (`evaluate.py`, default settings, `evaluation-results.json`)

1280×768 screens (wallpaper, taskbar, Explorer, dense console text) with a scrolled/moved second state, RDP8 v6 caches with cache hits and newest-first order. Only joins between *different* records are counted.

| Seed | Records | Precision | Recall | False joins | Largest region | Coherent regions ≥4 (tiles) |
| --- | --- | --- | --- | --- | --- | --- |
| 7 | 370 | 97.5% | 64.5% | 12 | 102 | 18 (222) |
| 11 | 373 | 98.2% | 59.2% | 8 | 141 | 9 (202) |
| 23 | 377 | 96.9% | 62.2% | 15 | 123 | 16 (234) |

At the same seeds, v1.1 made 112–131 correct and 21–30 false cross-record joins between 32-px subtiles: 81–86% precision, about 3.6–4.2k px correct versus 0.7–1.0k px false seam length. Its largest block was 32 subtiles (8 records). Version 1.2 joins about 28–30k px of correct seam at 97–98% precision.

Limitations measured: with 15% of records evicted, precision falls to 84–92% (recall 23–46%). With six caches mixed (`benchmark-results.json`), precision is 97.4% but recall falls to 35%. Lowering join confidence to 0.90 raises recall to about 68–71% at 96–97% precision.

### Real cache (`Cache0001.bin`, 6,412 native records)

There is no ground truth, so this is inspection only. Seam calibration on the evidence was: horizontal TPR 59% / FPR 1.4% at LLR ≥ 2; vertical 30% / 1.2%. EM cache-order rates were right-neighbour *k−1* 44%, *k+1* 13%, *k±2/3* ≈ 6%; vertical offsets were all ≤ 5%. At defaults, 1,035 records were joined into 468 regions (largest 6) in about 58 s; at confidence 0.90, 1,527 records into 645 regions. The top regions inspected visually were coherent: robocopy path lines, the Command Prompt title and header, and an Explorer file list. Most regions are horizontal runs. Vertical joins of terminal text are rarely supported by the evidence, and whole-window recovery was not achieved on this cache.

## Version 1.1 correction

**Version 1.1: 28 tests passed; static checks and compilation passed.** The new regression suite covers native duplicate/background restoration, contact-sheet inclusion, export with no inferred links, and streamed PNG pixel/alpha correctness. A 120.7-million-pixel streamed PNG was encoded, decoded and checked, reproducing the previously blocked size range. The benchmark below describes version 1.0; it is not a new measurement of 1.1.

# Verification record — original version 1.0 measurements

Verified 13 October 2026 in the supplied Linux environment, Python 3.12.14. The source targets Python 3.11+; Windows-specific runtime/display behaviour was not exercised in this environment.

- `python -m pytest -q tests`: **24 passed**, including offscreen PyQt6 tests with spawned workers.
- `python -m ruff check .`: **All checks passed**.
- `python -m compileall -q .`: passed.
- Headless CLI: a four-tile synthetic image produced a pixel-correct full canvas, block PNG, report and audit file.
- Three public `RDP8bmp` version 6 BIN files decoded **60,808 raw subtiles**, yielding **17,124 unique representatives**, **43,684 duplicate occurrences**, and **500 filtered background representatives**, with no BIN parser warnings.
- The first **100 native bitmap records** in a public BIN cache matched ANSSI bmc-tools extraction byte-for-byte.
- BMC raw depths 8/16/24/32, fixed slot offsets, orientation and interleaved compressed records are covered by generated fixtures. No genuine non-empty BMC fixture was available in the public sample archive.

## Final 10,000-tile benchmark

| Measurement | Result |
| --- | --- |
| Standalone ingestion | 9.84 seconds |
| Standalone matching/assembly | 11.82 seconds |
| GUI import + automatic assembly + item installation | 24.44 seconds |
| Canvas items | 10,000 |
| Inferred two-tile blocks | 5,000 |
| Matching synthetic pairs recovered | 5,000 / 5,000 |
| Qt heartbeat median | 16.15 ms |
| Qt heartbeat 95th percentile | 16.89 ms |
| Longest Qt heartbeat interval | 127.00 ms |

Full precision, workload description and platform are recorded in `benchmark-results.json`. This workload uses unique random tile interiors with one identical textured border per intended pair. Recovery on arbitrary RDP desktop material is not established by this synthetic result. Event-loop heartbeat is not a rendered-frame-rate measurement; occasional pauses remain, and uninterrupted 60fps is not guaranteed.

Public fixtures and temporary GUI inspection renders are excluded from the source archive. The application does not modify input evidence and requires no runtime network connection.
