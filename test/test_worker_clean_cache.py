from pathlib import Path

import pytest

from worker.clean_cache import format_bytes, get_cache_stats, main, purge_cache
from worker.store import Store


def setup_sample_cache(store: Store) -> tuple[str, Path, Path]:
    directory = store.files / "TestTitle"
    archive = store.files / "TestTitle.tar.gz"
    store.register_path("owner1", directory, "directory")
    store.register_path("owner1", archive, "file")
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "001_TestTitle.jpg"
    image.write_bytes(b"sample photo content")
    archive.write_bytes(b"sample archive content")

    payload = {
        "_files": [str(image), str(archive)],
        "_mediaFiles": {
            "m1": {"path": str(image), "sizeBytes": len(b"sample photo content"), "mimeType": "image/jpeg"},
            "m2": {"path": str(archive), "sizeBytes": len(b"sample archive content"), "mimeType": "application/gzip"},
        },
    }
    cid = store.publish("https://example.com/test", payload, directory, aliases=["https://short.url/t"], owner="owner1")
    return cid, directory, archive


def test_format_bytes() -> None:
    assert format_bytes(500) == "500 B"
    assert "KB" in format_bytes(2048)
    assert "MB" in format_bytes(10 * 1024 * 1024)
    assert "GB" in format_bytes(2 * 1024 * 1024 * 1024)


def test_get_cache_stats(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        cid, directory, archive = setup_sample_cache(store)
        stats = get_cache_stats(store)
        assert stats.total_entries == 1
        assert stats.total_bytes > 0
        assert stats.total_paths == 2
        assert stats.active_leases == 0
    finally:
        store.close()


def test_purge_dry_run_does_not_delete(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        cid, directory, archive = setup_sample_cache(store)
        res = purge_cache(store, dry_run=True)
        assert res["reclaimed_entries"] == 1
        assert res["dry_run"] is True
        assert directory.exists()
        assert archive.exists()
        assert store.lookup("https://example.com/test") is not None
    finally:
        store.close()


def test_purge_all(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        cid, directory, archive = setup_sample_cache(store)
        res = purge_cache(store, dry_run=False)
        assert res["reclaimed_entries"] == 1
        assert res["deleted_paths"] == 2
        assert not directory.exists()
        assert not archive.exists()
        assert store.lookup("https://example.com/test") is None
        stats = get_cache_stats(store)
        assert stats.total_entries == 0
        assert stats.total_paths == 0
    finally:
        store.close()


def test_purge_by_url_and_alias(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        cid, directory, archive = setup_sample_cache(store)
        # Purge using the alias
        res = purge_cache(store, target_url="https://short.url/t", dry_run=False)
        assert res["reclaimed_entries"] == 1
        assert not directory.exists()
        assert not archive.exists()
        assert store.lookup("https://example.com/test") is None
    finally:
        store.close()


def test_purge_respects_active_leases_unless_forced(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        cid, directory, archive = setup_sample_cache(store)
        _ = store.lease(cid)
        # Without force, active lease should be skipped
        res = purge_cache(store, force=False, dry_run=False)
        assert res["reclaimed_entries"] == 0
        assert res["skipped_active_leases"] == 1
        assert directory.exists()

        # With force, active lease is purged
        res_forced = purge_cache(store, force=True, dry_run=False)
        assert res_forced["reclaimed_entries"] == 1
        assert not directory.exists()
    finally:
        store.close()


def test_cli_main_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "test.db"
    store = Store(tmp_path, database_path=db_file, files_path=tmp_path / "downloads")
    setup_sample_cache(store)
    store.close()

    monkeypatch.setattr(
        "worker.clean_cache.resolve_store_paths",
        lambda: (tmp_path, tmp_path / "downloads", db_file, 1024 * 1024 * 1024),
    )

    # Test --stats
    exit_code = main(["--stats"])
    assert exit_code == 0

    # Test --dry-run
    exit_code = main(["--dry-run"])
    assert exit_code == 0

    # Test purge all
    exit_code = main(["--force"])
    assert exit_code == 0

    reopened = Store(tmp_path, database_path=db_file, files_path=tmp_path / "downloads")
    try:
        assert get_cache_stats(reopened).total_entries == 0
    finally:
        reopened.close()


def test_cli_online_dry_run_and_purge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "test.db"
    store = Store(tmp_path, database_path=db_file, files_path=tmp_path / "downloads")
    setup_sample_cache(store)
    store.close()
    monkeypatch.setattr(
        "worker.clean_cache.resolve_store_paths",
        lambda: (tmp_path, tmp_path / "downloads", db_file, 1024**3),
    )
    monkeypatch.setattr("worker.clean_cache.resolve_account_id", lambda: "123")

    assert main(["--online", "--dry-run"]) == 0
    assert main(["--online"]) == 0
    reopened = Store(tmp_path, database_path=db_file, files_path=tmp_path / "downloads")
    try:
        assert get_cache_stats(reopened).total_entries == 0
    finally:
        reopened.close()


def test_cli_refuses_while_worker_holds_the_data_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    db_file = tmp_path / "test.db"
    Store(tmp_path, database_path=db_file, files_path=tmp_path / "downloads").close()
    monkeypatch.setattr("worker.clean_cache.resolve_store_paths",
                        lambda: (tmp_path, tmp_path / "downloads", db_file, 1024**3))
    monkeypatch.setattr("worker.clean_cache.resolve_account_id", lambda: "123")
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    held = (sessions / "bot_123.worker.lock").open("a")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert main(["--stats"]) == 1
        assert main(["--stats", "--online"]) == 0
    finally:
        held.close()
    assert main(["--stats"]) == 0


def test_purge_keeps_delivery_receipts_and_matches_urls_by_job_key(tmp_path: Path) -> None:
    from worker.jobs import Jobs
    from worker.models import JobInput

    store = Store(tmp_path)
    try:
        request = JobInput(text="https://example.com/post", accountId="123", requestId="r", idempotencyKey="k")
        key = Jobs.key("https://example.com/post", request)
        directory = store.files / "Post"
        store.register_path("o", directory, "directory")
        directory.mkdir()
        (directory / "a.jpg").write_bytes(b"x")
        store.publish(key, {"_files": [str(directory / "a.jpg")]}, directory, owner="o")
        store.save_job({"id": "j", "status": "ready", "results": [],
                        "delivery": {"status": "sent", "messageIds": [1]}}, "k", "fp")
        res = purge_cache(store, target_url="https://example.com/post", account_id="123")
        assert res["reclaimed_entries"] == 1
        assert not (directory / "a.jpg").exists()
        purge_cache(store)
        assert store.idempotent("k", "fp")["delivery"]["status"] == "sent"
    finally:
        store.close()
