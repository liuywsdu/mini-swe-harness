"""宿主机侧只读访问 guest store（共享挂载上的 mini ReplayStore）。

store 是目录：``index.sqlite3`` + ``blobs/<sha256>.gz``。harness 用 stdlib
sqlite3/gzip 直接校验完整性（``session_snapshot.final`` 存在才算 episode
有效）并核对导出行数，不依赖 minisweagent 包。
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class GuestStoreError(RuntimeError):
    pass


class GuestStore:
    def __init__(self, store_dir: str | Path):
        self.store_dir = Path(store_dir)
        self.database = self.store_dir / "index.sqlite3"
        if not self.database.is_file():
            raise GuestStoreError(f"store database not found: {self.database}")

    def _read_blob(self, key: str) -> dict:
        path = self.store_dir / "blobs" / f"{key}.gz"
        try:
            return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except FileNotFoundError as exc:
            raise GuestStoreError(f"event payload blob missing: {path}") from exc

    def event_count(self, episode: str) -> int:
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            row = db.execute("SELECT COUNT(*) FROM events WHERE episode=?", (episode,)).fetchone()
        return int(row[0])

    def completed(self, episode: str) -> bool:
        """Episode 完整性门槛：存在 ``kind=session_snapshot`` 且 payload.final=true。"""
        with closing(sqlite3.connect(self.database, timeout=30)) as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE episode=? AND kind='session_snapshot'", (episode,)
            ).fetchall()
        return any(bool(self._read_blob(row[0]).get("final")) for row in rows)

    def exported_count(self, episode: str, events_path: str | Path) -> int:
        """校验 guest 侧 export-events 产物与 store 行数一致；返回导出行数。"""
        path = Path(events_path)
        if not path.is_file():
            raise GuestStoreError(f"guest_events export missing: {path}")
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        store_count = self.event_count(episode)
        if len(lines) != store_count:
            raise GuestStoreError(
                f"guest_events row mismatch: exported {len(lines)} vs store {store_count}"
            )
        return len(lines)
