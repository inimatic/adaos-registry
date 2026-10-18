"""Fail-closed, process-shared storage admission for derived media.

The ledger accounts *growth*, not existing originals. Completed allocations stay
charged until an explicit, verified deletion releases them. A crashed process
does not silently reclaim disk which may contain its partial output.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Callable, Mapping, Any

GIB = 1024**3
QUOTAS = {"database": 4 * GIB, "image": 2 * GIB,
          "av": 8 * GIB, "staging": 6 * GIB}


class StorageAdmission:
    """Serialize aggregate reservations; never infer capacity from source size."""

    def __init__(self, path: Path, *, capacity: Callable = shutil.disk_usage,
                 quotas: Mapping[str, int] | None = None,
                 system_floor: int = 4 * GIB, media_floor: int = 10 * GIB,
                 hysteresis: int = 512 * 1024**2):
        self.path = Path(path)
        self.capacity = capacity
        self.quotas = dict(QUOTAS if quotas is None else quotas)
        self.system_device = self.path.parent.stat().st_dev
        self.system_floor = system_floor
        self.media_floor = media_floor
        self.hysteresis = hysteresis
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS allocations (
                    owner TEXT PRIMARY KEY, state TEXT NOT NULL,
                    allocations TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pressure (
                    device TEXT PRIMARY KEY, blocked INTEGER NOT NULL);
            """)

    def _connect(self):
        # No 30-second interactive wait, including when another process owns
        # admission. All decisions/inserts below share BEGIN IMMEDIATE.
        return closing(sqlite3.connect(str(self.path), timeout=0.25))

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        candidate = path.resolve()
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        return candidate

    def reserve(self, owner: str, allocations: list[Mapping[str, Any]]) -> dict:
        try:
            return self._reserve(owner, allocations)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return {"ok": False, "state": "waiting_storage",
                    "reason": "capacity_unknown"}

    def _reserve(self, owner: str, allocations: list[Mapping[str, Any]]) -> dict:
        if not owner or not allocations:
            raise ValueError("storage_reservation_identity_required")
        requested = []
        probes = {}
        for value in allocations:
            category = str(value["category"])
            size = int(value["bytes"])
            if category not in self.quotas or size <= 0:
                raise ValueError("storage_reservation_invalid")
            parent = self._existing_parent(Path(value["path"]))
            device = str(parent.stat().st_dev)
            probes[device] = parent
            requested.append({"category": category, "bytes": size, "device": device})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT allocations FROM allocations WHERE owner=?", (owner,)
            ).fetchone()
            if existing:
                if json.loads(existing[0]) != requested:
                    raise ValueError("storage_reservation_identity_conflict")
                return {"ok": True, "state": "reserved", "reused": True}
            category_totals = {key: 0 for key in self.quotas}
            device_reserved = {}
            for state, raw in connection.execute("SELECT state,allocations FROM allocations"):
                for item in json.loads(raw):
                    category_totals[item["category"]] += item["bytes"]
                    # Committed bytes are already reflected in disk.free.
                    if state == "reserved":
                        device_reserved[item["device"]] = (
                            device_reserved.get(item["device"], 0) + item["bytes"])
            for item in requested:
                category_totals[item["category"]] += item["bytes"]
                device_reserved[item["device"]] = (
                    device_reserved.get(item["device"], 0) + item["bytes"])
            if any(category_totals[key] > limit for key, limit in self.quotas.items()):
                return {"ok": False, "state": "waiting_storage",
                        "reason": "growth_budget_exhausted"}
            for device, parent in probes.items():
                free = int(self.capacity(parent).free)
                if free < 0:
                    raise ValueError("storage_capacity_invalid")
                pressure = connection.execute(
                    "SELECT blocked FROM pressure WHERE device=?", (device,)
                ).fetchone()
                floor = (self.system_floor if device == str(self.system_device)
                         else self.media_floor)
                required = floor + device_reserved[device]
                if pressure and pressure[0]:
                    required += self.hysteresis
                if free < required:
                    connection.execute("INSERT OR REPLACE INTO pressure VALUES (?,1)", (device,))
                    connection.commit()
                    return {"ok": False, "state": "waiting_storage",
                            "reason": "free_space_floor", "free_bytes": free,
                            "required_bytes": required}
            for device in probes:
                connection.execute("INSERT OR REPLACE INTO pressure VALUES (?,0)", (device,))
            connection.execute("INSERT INTO allocations VALUES (?,'reserved',?,?)",
                               (owner, json.dumps(requested, sort_keys=True), time.time()))
            connection.commit()
        return {"ok": True, "state": "reserved", "reused": False}

    def commit(self, owner: str, *, output_bytes: int) -> None:
        """Remove staging charge; retain only verified permanent output growth."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT allocations FROM allocations WHERE owner=?", (owner,)).fetchone()
            if row is None:
                raise ValueError("storage_reservation_missing")
            values = [item for item in json.loads(row[0]) if item["category"] != "staging"]
            if len(values) != 1 or not 0 <= output_bytes <= values[0]["bytes"]:
                raise ValueError("storage_output_exceeds_reservation")
            values[0]["bytes"] = output_bytes
            connection.execute("UPDATE allocations SET state='committed',allocations=?,updated=? WHERE owner=?",
                               (json.dumps(values, sort_keys=True), time.time(), owner))
            connection.commit()

    def release(self, owner: str) -> None:
        """Caller must first prove that owned output/partials were removed."""
        with self._connect() as connection:
            connection.execute("DELETE FROM allocations WHERE owner=?", (owner,))
            connection.commit()


__all__ = ["StorageAdmission", "QUOTAS"]
