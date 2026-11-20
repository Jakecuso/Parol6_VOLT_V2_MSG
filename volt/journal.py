"""volt/journal.py — VOLT's memory journal (Pi side).

Every meaningful thing that happens is appended as one JSON line to
data/memory/journal.jsonl. The Dell Brain pulls new entries
(GET /api/memory?since=<id>), embeds them, and uses them as long-term memory.
The Pi never embeds anything; it only records.

Entry: {"id": int, "t": unix_time, "kind": str, "text": str, "data": {...}}

`text` is a plain-English sentence written for the embedding model and for
you. `data` holds the exact values (angles, offsets, settings before/after).

Kinds: pose_saved, pose_deleted, pose_used, homed, gripper_calibrated,
settings_changed, enabled, stop, estop, refused, ai_command, ai_executed, note.
"""
from __future__ import annotations

import json
import threading
import time

from .config import DATA

JOURNAL = DATA / "memory" / "journal.jsonl"


class Journal:
    def __init__(self, path=JOURNAL):
        self.path = path
        self._lock = threading.Lock()
        self._next = self._last_id() + 1

    def _last_id(self) -> int:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                lines = f.read().splitlines()
            return json.loads(lines[-1])["id"] if lines else 0
        except Exception:
            return 0

    def add(self, kind: str, text: str, **data) -> dict:
        with self._lock:
            e = {"id": self._next, "t": round(time.time(), 3), "kind": kind, "text": text, "data": data}
            self._next += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(e) + "\n")
            return e

    def since(self, after_id: int = 0, limit: int = 1000) -> list[dict]:
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e["id"] > after_id:
                        out.append(e)
                        if len(out) >= limit:
                            break
        except FileNotFoundError:
            pass
        return out
