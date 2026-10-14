from __future__ import annotations

import multiprocessing as mp
import time
from dataclasses import replace
from threading import Event

from engine import stitch
from models import Cancelled, IngestResult, check_cancel
from parser import ingest


def _child(operation, parameters, cancel, pipe):
    last = 0.0

    def progress(message, current, total):
        nonlocal last
        now = time.monotonic()
        if now - last > 0.10 or current == total:
            last = now
            pipe.send(("progress", (message, current, total)))

    try:
        if parameters.pop("_stream_tiles", False):
            parameters["tiles"] = []
            while True:
                check_cancel(cancel)
                kind, chunk = pipe.recv()
                if kind == "tiles_done":
                    break
                parameters["tiles"].extend(chunk)
        if operation == "ingest":
            result = ingest(**parameters, cancel=cancel, progress=progress)
        elif operation == "stitch":
            result = stitch(**parameters, cancel=cancel, progress=progress)
        else:
            raise ValueError(f"Unknown worker operation: {operation}")
        if isinstance(result, IngestResult):
            pipe.send(("header", replace(result, tiles=[], sources=[], warnings=[], colour_order=[])))
            fields = [
                ("tiles", result.tiles, 128),
                ("sources", result.sources, 128),
                ("warnings", result.warnings, 128),
                ("colour_order", result.colour_order, 1024),
            ]
        else:
            pipe.send(("header", replace(result, placements={}, links=[], unplaced=[], instances=[])))
            fields = [
                ("instances", result.instances, 128),
                ("placements", list(result.placements.values()), 256),
                ("links", result.links, 256),
                ("unplaced", result.unplaced, 1024),
            ]
        for field, values, batch in fields:
            for start in range(0, len(values), batch):
                check_cancel(cancel)
                pipe.send(("chunk", (field, values[start : start + batch])))
        pipe.send(("result_done", None))
    except Cancelled:
        pipe.send(("cancelled", None))
    except Exception as error:
        pipe.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        pipe.close()


def run_process(operation: str, parameters: dict, cancel: Event, progress):
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    stop = context.Event()
    parameters = dict(parameters)
    streamed_tiles = parameters.pop("tiles", None)
    if streamed_tiles is not None:
        parameters["_stream_tiles"] = True
    process = context.Process(target=_child, args=(operation, parameters, stop, sender), daemon=True)
    process.start()
    sender.close()
    try:
        if streamed_tiles is not None:
            for start in range(0, len(streamed_tiles), 128):
                check_cancel(cancel)
                receiver.send(("tiles", streamed_tiles[start : start + 128]))
            receiver.send(("tiles_done", None))
        result = None
        while True:
            if cancel.is_set():
                stop.set()
            if receiver.poll(0.02):
                try:
                    kind, payload = receiver.recv()
                except EOFError as error:
                    raise RuntimeError("Background process closed without a result") from error
                if kind == "progress":
                    progress(*payload)
                elif kind == "header":
                    result = payload
                elif kind == "chunk":
                    field, chunk = payload
                    if field == "placements":
                        result.placements.update({p.tile_id: p for p in chunk})
                    else:
                        getattr(result, field).extend(chunk)
                elif kind == "result_done":
                    if cancel.is_set():
                        raise Cancelled("Operation cancelled")
                    return result
                elif kind == "cancelled":
                    raise Cancelled("Operation cancelled")
                else:
                    raise RuntimeError(payload)
            elif not process.is_alive():
                raise RuntimeError(f"Background process exited with code {process.exitcode}")
    finally:
        stop.set()
        receiver.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        process.close()
