"""Worker cache maintenance and cleanup CLI.

Callable inside containers via:
    docker exec -it parsehub-worker python -m worker.clean_cache [OPTIONS]
or via docker compose:
    docker compose -f compose.worker.yaml exec parsehub-worker python -m worker.clean_cache [OPTIONS]
"""
import argparse
import fcntl
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import make_url

from worker.store import Store


def format_bytes(size: float | int) -> str:
    """Format byte counts into human-readable strings."""
    num = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.2f} B"


@dataclass
class CacheStats:
    total_entries: int
    total_bytes: int
    total_paths: int
    active_leases: int
    total_jobs: int


def candidate_keys(url: str, account_id: str | None) -> list[str]:
    """Mirror Jobs.key() for every transport/output combination of the given URL."""
    if not account_id:
        return []
    return [
        hashlib.sha256(f"v7-original-pipeline:{transport}:{account_id}:{mode}:{url}".encode()).hexdigest()
        for transport in ("direct", "files")
        for mode in ("preview", "raw", "zip")
    ]


def resolve_account_id() -> str | None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        try:
            from worker.config import WorkerSettings

            token = WorkerSettings().bot_token.get_secret_value()  # type: ignore[call-arg]
        except Exception:
            return None
    return token.split(":", 1)[0] if token else None


def resolve_store_paths() -> tuple[Path, Path, Path, int]:
    """Resolve data path, download directory, database path, and max cache size.

    Attempts to load from WorkerSettings first; gracefully falls back to environment
    variables and sensible defaults so cleanup can succeed even if bot credentials
    are not present.
    """
    try:
        from worker.config import WorkerSettings

        settings = WorkerSettings()  # type: ignore[call-arg]
        return (
            settings.data_path,
            settings.download_dir,
            settings.database_path,
            settings.worker_cache_max_bytes,
        )
    except Exception:
        data_path = Path(os.environ.get("DATA_PATH", "data"))
        download_dir = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
        db_url_str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data/db/database.db")
        url = make_url(db_url_str)
        db_path = Path(url.database) if url.database else data_path / "db" / "database.db"
        max_bytes = int(os.environ.get("WORKER_CACHE_MAX_BYTES", 10 * 1024**3))
        return data_path, download_dir, db_path, max_bytes


def get_cache_stats(store: Store) -> CacheStats:
    """Inspect current Worker cache usage."""
    now = time.time()
    cache_rows = store.db.execute("SELECT id, bytes FROM worker_cache").fetchall()
    total_entries = len(cache_rows)
    total_bytes = sum(int(row["bytes"]) for row in cache_rows)
    total_paths = store.db.execute("SELECT COUNT(*) FROM worker_owned_paths").fetchone()[0]
    active_leases = store.db.execute(
        "SELECT COUNT(*) FROM worker_leases WHERE expires > ?", (now,)
    ).fetchone()[0]
    total_jobs = store.db.execute("SELECT COUNT(*) FROM worker_jobs").fetchone()[0]
    return CacheStats(
        total_entries=total_entries,
        total_bytes=total_bytes,
        total_paths=total_paths,
        active_leases=active_leases,
        total_jobs=total_jobs,
    )


def purge_cache(
    store: Store,
    target_url: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Reclaim cache items, owned disk files, and metadata.

    Args:
        store: Worker Store instance.
        target_url: Specific URL or key to purge. If None, purges all cache.
        force: If True, purges active leases as well.
        dry_run: If True, inspects but performs no modifications.
    """
    now = time.time()
    active_lease_cache_ids: set[str] = set()
    if not force:
        lease_rows = store.db.execute(
            "SELECT cache_id FROM worker_leases WHERE expires > ?", (now,)
        ).fetchall()
        active_lease_cache_ids = {row["cache_id"] for row in lease_rows}

    target_keys: list[str] = []
    if target_url:
        # Cache keys and aliases are Jobs.key() digests, never raw URLs; accept both forms.
        probes = [target_url, *candidate_keys(target_url, account_id)]
        placeholders = ",".join("?" * len(probes))
        alias_rows = store.db.execute(
            f"SELECT key FROM worker_aliases WHERE alias IN ({placeholders})", probes
        ).fetchall()
        target_keys = list(dict.fromkeys([*probes, *(row[0] for row in alias_rows)]))
        placeholders = ",".join("?" * len(target_keys))
        cache_rows = store.db.execute(
            f"SELECT * FROM worker_cache WHERE key IN ({placeholders})", target_keys
        ).fetchall()
    else:
        cache_rows = store.db.execute("SELECT * FROM worker_cache").fetchall()

    target_cache_ids: list[str] = []
    skipped_leases = 0
    reclaimed_bytes = 0

    for row in cache_rows:
        cid = row["id"]
        if cid in active_lease_cache_ids:
            skipped_leases += 1
            continue
        target_cache_ids.append(cid)
        reclaimed_bytes += int(row["bytes"])

    owned_rows = []
    for cid in target_cache_ids:
        rows = store.db.execute(
            "SELECT * FROM worker_owned_paths WHERE cache_id=?", (cid,)
        ).fetchall()
        owned_rows.extend(rows)

    if dry_run:
        return {
            "reclaimed_entries": len(target_cache_ids),
            "reclaimed_bytes": reclaimed_bytes,
            "owned_paths_count": len(owned_rows),
            "skipped_active_leases": skipped_leases,
            "dry_run": True,
        }

    # Perform actual removal
    deleted_paths = 0
    for row in owned_rows:
        if store._remove_owned(row):
            deleted_paths += 1

    for cid in target_cache_ids:
        store.db.execute("DELETE FROM worker_cache WHERE id=?", (cid,))

    if force:
        store.db.execute("DELETE FROM worker_leases")

    if target_url:
        placeholders = ",".join("?" * len(target_keys))
        store.db.execute(f"DELETE FROM worker_aliases WHERE alias IN ({placeholders}) OR key IN ({placeholders})",
                         [*target_keys, *target_keys])
    else:
        store.db.execute("DELETE FROM worker_aliases")
    # worker_jobs holds durable delivery receipts that idempotent re-queries rely on;
    # Store.cleanup() ages them out on its own schedule.

    store.db.commit()

    # Reclaim fragmented space in SQLite database
    try:
        store.db.execute("VACUUM")
    except Exception:
        pass

    return {
        "reclaimed_entries": len(target_cache_ids),
        "reclaimed_bytes": reclaimed_bytes,
        "deleted_paths": deleted_paths,
        "skipped_active_leases": skipped_leases,
        "dry_run": False,
    }


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m worker.clean_cache",
        description="清理 ParseHub Worker 缓存与已登记的落盘媒体文件 (可在容器内直接运行)",
    )
    parser.add_argument(
        "-a", "--all",
        action="store_true",
        default=True,
        help="清理所有 Worker 缓存 (默认操作)",
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        default=None,
        help="仅清理指定 URL 或别名的缓存记录与对应文件",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        default=False,
        help="强制清理正在使用的活动租约 (默认会跳过活动租约以保护正在传输的任务)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        default=False,
        help="演练模式：仅查看将被清理的内容和占用容量，不实际删除",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        default=False,
        help="仅查看当前 Worker 缓存统计信息，不执行任何清理",
    )
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    opts = parse_args(args)
    data_path, download_dir, db_path, max_bytes = resolve_store_paths()

    if not db_path.is_file():
        print(f"[Worker Cache Cleaner] 数据库文件未找到: {db_path}，当前无 Worker 缓存。")
        return 0

    # Share the running Worker's data lock: recovery and purges must never race a live process.
    sessions = data_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    account_id = resolve_account_id()
    lock_name = f"bot_{account_id}.worker.lock" if account_id else "worker.lock"
    lock = (sessions / lock_name).open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("[Worker Cache Cleaner] Worker 正在运行，请先停止 Worker 再执行清理。")
        return 1

    store = Store(data_path, max_bytes, database_path=db_path, files_path=download_dir, recover=False)

    try:
        stats = get_cache_stats(store)
        print("=" * 50)
        print("  ParseHub Worker 缓存维护工具")
        print("=" * 50)
        print(f"数据路径:    {data_path.resolve()}")
        print(f"下载目录:    {download_dir.resolve()}")
        print(f"数据库文件:  {db_path.resolve()}")
        print("-" * 50)
        print(f"当前缓存条目:    {stats.total_entries} 条")
        print(f"占用磁盘容量:    {format_bytes(stats.total_bytes)}")
        print(f"登记管理路径:    {stats.total_paths} 处 (目录/文件)")
        print(f"活跃传输租约:    {stats.active_leases} 个")
        print(f"历史任务记录:    {stats.total_jobs} 条")
        print("-" * 50)

        if opts.stats:
            return 0

        if opts.dry_run:
            print("【模式: 演练 (DRY RUN) - 不会实际修改或删除任何数据】")

        if opts.url:
            print(f"目标清理对象: 指定链接 [{opts.url}]")
        else:
            print("目标清理对象: 全部 Worker 缓存")

        res = purge_cache(
            store,
            target_url=opts.url,
            force=opts.force,
            dry_run=opts.dry_run,
            account_id=account_id,
        )

        if opts.dry_run:
            print(f"预计清理条目:    {res['reclaimed_entries']} 条")
            print(f"预计释放空间:    {format_bytes(res['reclaimed_bytes'])}")
            print(f"预计删除路径:    {res['owned_paths_count']} 处")
            if res["skipped_active_leases"] > 0:
                print(f"注意: 有 {res['skipped_active_leases']} 条缓存因存在活跃租约被跳过 (可用 -f/--force 强制清理)")
        else:
            print(f"已清理缓存条目:  {res['reclaimed_entries']} 条")
            print(f"已释放磁盘空间:  {format_bytes(res['reclaimed_bytes'])}")
            print(f"已删除磁盘路径:  {res['deleted_paths']} 处")
            if res["skipped_active_leases"] > 0:
                print(f"提示: {res['skipped_active_leases']} 条缓存因活跃租约被跳过 (可用 -f/--force 强制清理)")
            print("数据库状态:      SQLite 已执行整理压缩 (VACUUM)")
            print("结果:            清理已顺利完成！")

        return 0
    finally:
        store.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
