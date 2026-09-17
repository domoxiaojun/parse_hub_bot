"""Original names are independent of ownership; only registered paths are reclaimed."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from worker.store import TTL, Store


def test_title_directory_and_sibling_archive_share_lease(tmp_path: Path) -> None:
    store = Store(tmp_path)
    directory = store.files / "Original title"
    archive = store.files / "Original title.tar.gz"
    store.register_path("owner", directory, "directory")
    store.register_path("owner", archive, "file")
    directory.mkdir()
    image = directory / "001_Original title.jpg"
    image.write_bytes(b"photo")
    archive.write_bytes(b"archive")
    result = {"_files": [str(image), str(archive)], "_mediaFiles": {
        "archive": {"path": str(archive), "sizeBytes": 7, "mimeType": "application/gzip"},
    }}
    with patch("worker.store.time.time", return_value=100):
        cache = store.publish("key", result, directory, owner="owner")
    with patch("worker.store.time.time", return_value=100 + TTL - 1):
        lease = store.lease(cache)
    with patch("worker.store.time.time", return_value=100 + TTL):
        assert store.lookup("key") is None
        store.cleanup()
        assert image.exists() and archive.exists()
        assert store.media_file(lease, "archive") is not None
        store.release(lease)
        store.cleanup()
        assert not directory.exists() and not archive.exists()
    store.close()


def test_startup_only_cleans_registered_unpublished_paths(tmp_path: Path) -> None:
    store = Store(tmp_path)
    abandoned = store.files / "Native title"
    archive = store.files / "Native title.tar.gz"
    store.register_path("interrupted", abandoned, "directory")
    store.register_path("interrupted", archive, "file")
    abandoned.mkdir()
    (abandoned / "Native title.mp4").write_bytes(b"partial")
    archive.write_bytes(b"partial")
    legacy = store.files / "worker-legacy-user-file"
    legacy.mkdir()
    (legacy / "old.mp4").write_bytes(b"keep")
    store.close()
    reopened = Store(tmp_path)
    assert not abandoned.exists() and not archive.exists()
    assert (legacy / "old.mp4").read_bytes() == b"keep"
    reopened.close()


def test_cancel_cannot_remove_another_preparation_or_existing_file(tmp_path: Path) -> None:
    store = Store(tmp_path)
    one, two = store.files / "Title", store.files / "Title_2"
    store.register_path("one", one, "directory")
    one.mkdir()
    store.register_path("two", two, "directory")
    two.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        store.register_path("three", one, "directory")
    with pytest.raises(sqlite3.IntegrityError):
        other = store.files / "Reserved"
        store.register_path("one", other, "directory")
        store.register_path("two", other, "directory")
    store.discard_preparation("one")
    assert not one.exists() and two.exists()
    store.close()


def test_completed_original_pipeline_directory_can_be_adopted(tmp_path: Path) -> None:
    store = Store(tmp_path)
    directory = store.files / "Original pipeline output"
    directory.mkdir()
    (directory / "media.jpg").write_bytes(b"media")
    store.register_path("owner", directory, "directory", existing=True)
    cache = store.publish("key", {"_files": [str(directory / "media.jpg")]}, directory, owner="owner")
    assert store.lookup("key")[0] == cache
    store.close()


def test_no_root_or_unregistered_file_can_be_published_or_removed(tmp_path: Path) -> None:
    store = Store(tmp_path)
    legacy = store.files / "history.mp4"
    legacy.write_bytes(b"keep")
    with pytest.raises(ValueError, match="invalid preparation"):
        store.register_path("owner", store.files, "directory")
    with pytest.raises(ValueError, match="unregistered"):
        store.publish("key", {"_files": [str(legacy)]}, store.files, owner="owner")
    store.discard_preparation("owner")
    store.cleanup()
    assert legacy.read_bytes() == b"keep"
    store.close()
