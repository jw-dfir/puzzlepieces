from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.events: list[dict] = []
        self._previous = "0" * 64
        self._stream = self.path.open("x", encoding="utf-8", buffering=1)
        self.record("start", version="1.1.0", score_semantics="heuristic similarity, not calibrated probability")

    def record(self, action: str, **fields) -> dict:
        with self._lock:
            event = {
                "sequence": len(self.events),
                "utc": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "previous_sha256": self._previous,
                **fields,
            }
            canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            event["event_sha256"] = digest
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.events.append(event)
            self._previous = digest
            return event

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            os.fsync(self._stream.fileno())

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        with self._lock:
            if not self._stream.closed:
                self._stream.flush()
                self._stream.close()


def verify_audit(path: str | Path) -> bool:
    previous = "0" * 64
    seen = False
    for index, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
            digest = event.pop("event_sha256")
            if event["previous_sha256"] != previous or event["sequence"] != index:
                return False
            canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
                return False
            previous = digest
            seen = True
        except (ValueError, KeyError, TypeError):
            return False
    return seen
