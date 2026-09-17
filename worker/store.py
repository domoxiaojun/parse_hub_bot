"""SQLite metadata and fixed-expiry file cache with independent leases."""
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, cast

TTL = 172800
LEASE_TTL = 300
# A freshly published, non-reusable result is leased by its waiter only after the
# shared task returns; keep it out of cleanup for this long.
PUBLISH_GRACE = 120


class Store:
    def __init__(self, root: Path, max_bytes: int = 10 * 1024**3,
                 database_path: Path | None = None, files_path: Path | None = None,
                 recover: bool = True) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = (files_path or self.root / "files").resolve()
        self.files.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.database_path = database_path or self.root / "worker.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.database_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS worker_jobs (
              id TEXT PRIMARY KEY, idem TEXT UNIQUE, fingerprint TEXT, payload TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS worker_cache (
              id TEXT PRIMARY KEY, key TEXT, payload TEXT, directory TEXT, bytes INTEGER,
              expires REAL, accessed REAL);
            CREATE INDEX IF NOT EXISTS worker_cache_key ON worker_cache(key);
            CREATE TABLE IF NOT EXISTS worker_aliases (alias TEXT PRIMARY KEY, key TEXT);
            CREATE TABLE IF NOT EXISTS worker_leases (id TEXT PRIMARY KEY, cache_id TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS worker_owned_paths (
              path TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, cache_id TEXT);
        """)
        # Older databases predate the publish timestamp; add it in place.
        if "created" not in {r[1] for r in self.db.execute("PRAGMA table_info(worker_cache)")}:
            self.db.execute("ALTER TABLE worker_cache ADD COLUMN created REAL")
            self.db.execute("UPDATE worker_cache SET created=accessed WHERE created IS NULL")
            self.db.commit()
        if recover:
            self.recover()

    def recover(self) -> None:
        """Mark work left by a dead Worker; only the process that owns the data lock may call this."""
        for row in self.db.execute("SELECT id,payload FROM worker_jobs").fetchall():
            payload = json.loads(row["payload"])
            if payload["status"] in {"queued", "running"}:
                delivery = payload.get("delivery")
                if delivery and delivery.get("status") == "sent":
                    payload.update(status="ready", stage="delivered")
                    self.save_job(payload)
                    continue
                if delivery:
                    delivery["status"] = "unknown" if delivery.get("inFlight") else (
                        "partial" if delivery.get("completedFrames") else "cancelled")
                payload.update(status="interrupted", error={"code": "worker_interrupted", "message": "任务已中断"})
                self.save_job(payload)
        self.db.commit()
        # Only journaled, unpublished outputs belong to interrupted preparation.
        owners = self.db.execute("SELECT DISTINCT owner FROM worker_owned_paths WHERE cache_id IS NULL").fetchall()
        for row in owners:
            self.discard_preparation(row[0])

    def managed_directory(self, directory: Path) -> bool:
        return (directory.parent.resolve() == self.files and not directory.is_symlink() and directory.is_dir()
                and self.db.execute("SELECT 1 FROM worker_owned_paths WHERE path=? AND kind='directory'",
                                    (str(directory.resolve()),)).fetchone() is not None)

    def register_path(self, owner: str, path: Path, kind: str, *, existing: bool = False) -> None:
        if kind not in {"directory", "file"} or path.parent.resolve() != self.files or path.is_symlink():
            raise ValueError("invalid preparation path")
        path = path.resolve()
        valid_existing = (kind == "directory" and path.is_dir()) or (kind == "file" and path.is_file())
        if existing and not valid_existing:
            raise ValueError("preparation path does not exist")
        if not existing and path.exists():
            raise ValueError("preparation path already exists")
        self.db.execute("INSERT INTO worker_owned_paths VALUES (?,?,?,NULL)", (str(path), kind, owner))
        self.db.commit()

    def _remove_owned(self, row: sqlite3.Row) -> bool:
        path = Path(row["path"])
        if path.parent.resolve() != self.files or path.is_symlink():
            return False
        try:
            if row["kind"] == "directory" and path.is_dir():
                shutil.rmtree(path)
            elif row["kind"] == "file":
                path.unlink(missing_ok=True)
            elif path.exists():
                return False
        except OSError:
            return False
        self.db.execute("DELETE FROM worker_owned_paths WHERE path=?", (str(path),))
        return True

    def discard_preparation(self, owner: str) -> None:
        for row in self.db.execute("SELECT * FROM worker_owned_paths WHERE owner=? AND cache_id IS NULL",
                                   (owner,)).fetchall():
            self._remove_owned(row)
        self.db.commit()

    def _file_owned(self, path: Path, cache_id: str | None = None, owner: str | None = None) -> bool:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.files):
            return False
        rows = (self.db.execute("SELECT * FROM worker_owned_paths WHERE cache_id IS NULL AND owner=?",
                               (owner,)).fetchall() if owner is not None else
                self.db.execute("SELECT * FROM worker_owned_paths WHERE cache_id=?", (cache_id,)).fetchall())
        for row in rows:
            root = Path(row["path"])
            if root.is_symlink():
                continue
            if (row["kind"] == "file" and path.resolve() == root
                    or row["kind"] == "directory" and path.resolve().is_relative_to(root)):
                return True
        return False

    def close(self) -> None:
        self.db.close()

    def save_job(self, payload: dict[str, Any], idem: str = "", fingerprint: str = "") -> None:
        existing = self.db.execute("SELECT id FROM worker_jobs WHERE id=?", (payload["id"],)).fetchone()
        if existing:
            self.db.execute("UPDATE worker_jobs SET payload=?,updated=? WHERE id=?",
                            (json.dumps(payload), time.time(), payload["id"]))
        else:
            self.db.execute("INSERT INTO worker_jobs VALUES (?,?,?,?,?)",
                            (payload["id"], idem or None, fingerprint, json.dumps(payload), time.time()))
        self.db.commit()

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT payload FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return cast(dict[str, Any], json.loads(row[0])) if row else None

    def idempotent(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT fingerprint,payload FROM worker_jobs WHERE idem=?", (key,)).fetchone()
        if not row:
            return None
        if row[0] != fingerprint:
            raise ValueError("idempotency_conflict")
        return cast(dict[str, Any], json.loads(row[1]))

    def lookup(self, key: str) -> tuple[str, dict[str, Any]] | None:
        alias = self.db.execute("SELECT key FROM worker_aliases WHERE alias=?", (key,)).fetchone()
        key = alias[0] if alias else key
        row = self.db.execute("SELECT * FROM worker_cache WHERE key=? AND expires>? ORDER BY expires DESC LIMIT 1",
                              (key, time.time())).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        if any(not self._file_owned(Path(path), cache_id=row["id"]) for path in payload.get("_files", [])):
            return None
        self.db.execute("UPDATE worker_cache SET accessed=? WHERE id=?", (time.time(), row["id"]))
        self.db.commit()
        return row["id"], payload

    def remember_alias(self, alias: str, key: str) -> None:
        if alias != key:
            self.db.execute("INSERT OR REPLACE INTO worker_aliases VALUES (?,?)", (alias, key))
            self.db.commit()

    def publish(self, key: str, result: dict[str, Any], directory: Path,
                aliases: list[str] | None = None, reusable: bool = True, owner: str | None = None) -> str:
        cache_id = uuid.uuid4().hex
        files = [Path(path) for path in result.get("_files", [])]
        if any(not self._file_owned(path, owner=owner) for path in files):
            raise ValueError("unregistered cache file")
        size = sum(path.stat().st_size for path in set(files))
        now = time.time()
        self.db.execute("INSERT INTO worker_cache (id,key,payload,directory,bytes,expires,accessed,created) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (cache_id, key, json.dumps(result), str(directory), size,
                         now + TTL if reusable else now, now, now))
        if owner is not None:
            self.db.execute("UPDATE worker_owned_paths SET cache_id=? WHERE owner=? AND cache_id IS NULL",
                            (cache_id, owner))
        for alias in aliases or []:
            self.db.execute("INSERT OR REPLACE INTO worker_aliases VALUES (?,?)", (alias, key))
        self.db.commit()
        return cache_id

    def lease(self, cache_id: str) -> str:
        lease_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO worker_leases VALUES (?,?,?)", (lease_id, cache_id, time.time() + LEASE_TTL))
        self.db.commit()
        return lease_id

    def renew(self, lease_id: str) -> bool:
        cursor = self.db.execute("UPDATE worker_leases SET expires=? WHERE id=? AND expires>?",
                                 (time.time() + LEASE_TTL, lease_id, time.time()))
        self.db.commit()
        return cursor.rowcount > 0

    def release(self, lease_id: str) -> None:
        self.db.execute("DELETE FROM worker_leases WHERE id=?", (lease_id,))
        self.db.commit()

    def media_file(self, lease_id: str, media_id: str) -> tuple[Path, str, int] | None:
        row = self.db.execute(
            "SELECT c.payload,c.id FROM worker_leases l JOIN worker_cache c ON c.id=l.cache_id "
            "WHERE l.id=? AND l.expires>?", (lease_id, time.time()),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row['payload'])
        item = payload.get('_mediaFiles', {}).get(media_id)
        if not item:
            return None
        path = Path(item['path'])
        if (not self._file_owned(path, cache_id=row['id'])
                or path.stat().st_size != item['sizeBytes']):
            return None
        return path, item['mimeType'], item['sizeBytes']

    def cleanup(self) -> None:
        now = time.time()
        self.db.execute("DELETE FROM worker_leases WHERE expires<=?", (now,))
        rows = self.db.execute("SELECT * FROM worker_cache ORDER BY accessed ASC").fetchall()
        total = sum(row["bytes"] for row in rows)
        for row in rows:
            if row["expires"] > now and total <= self.max_bytes:
                continue
            if (row["created"] or 0) > now - PUBLISH_GRACE:
                continue
            if self.db.execute("SELECT 1 FROM worker_leases WHERE cache_id=?", (row["id"],)).fetchone():
                continue
            owned = self.db.execute("SELECT * FROM worker_owned_paths WHERE cache_id=?", (row["id"],)).fetchall()
            removed = [self._remove_owned(item) for item in owned]
            if not all(removed):
                continue
            self.db.execute("DELETE FROM worker_cache WHERE id=?", (row["id"],))
            total -= row["bytes"]
        self.db.execute("DELETE FROM worker_jobs WHERE updated<?", (now - TTL,))
        self.db.execute("DELETE FROM worker_aliases WHERE key NOT IN (SELECT key FROM worker_cache)")
        self.db.commit()
