"""Static deployment contract checks; no Docker command, image build or bot login."""
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from worker.config import WorkerSettings
from worker.healthcheck import main

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {"bot_token": "123:fixture", "api_id": 1, "api_hash": "fixture",
            "worker_service_key": "fixture-service-key-32-characters-long", "_env_file": None}


def test_worker_container_bind_requires_explicit_opt_in() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert WorkerSettings(**SETTINGS).worker_host == "127.0.0.1"
        with pytest.raises(ValidationError):
            WorkerSettings(**SETTINGS, worker_host="0.0.0.0")
        assert WorkerSettings(**SETTINGS, worker_host="0.0.0.0", worker_allow_container_bind=True)
        with pytest.raises(ValidationError):
            WorkerSettings(**SETTINGS, worker_host="192.168.1.2", worker_allow_container_bind=True)


def test_compose_builds_repository_and_runs_only_worker() -> None:
    config = yaml.safe_load((ROOT / "compose.worker.yaml").read_text())
    assert set(config["services"]) == {"parsehub-worker"}
    service = config["services"]["parsehub-worker"]
    assert service["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert service["command"] == ["python", "-m", "worker"]
    assert service["env_file"] == [".env"]
    assert service["ports"] == ["127.0.0.1:${PARSEHUB_WORKER_HTTP_PORT:-8080}:8080"]
    assert service["volumes"] == ["./data:/app/data", "./downloads:/app/downloads", "./logs:/app/logs"]
    assert service["environment"]["WORKER_ALLOW_CONTAINER_BIND"] == "true"
    assert config["networks"]["parsehub"]["name"] == "parsehub-worker-network"
    assert service["healthcheck"]["test"] == ["CMD", "python", "-m", "worker.healthcheck"]
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert all(item in ignored for item in (".env", ".env.*", "data", "**/*.session", ".git"))


def test_worker_reuses_original_paths_without_creating_them(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        settings = WorkerSettings(**SETTINGS)
        assert settings.data_path == Path("data")
        assert settings.database_path == Path("data/db/database.db")
        assert settings.download_dir == Path("downloads")
        assert settings.platform_config_path == Path("data/config/platform_config.yaml")
        assert settings.sessions_path / f"{settings.bot_session_name}.session" == Path("data/sessions/bot_123.session")
        custom = WorkerSettings(**SETTINGS, data_path=tmp_path / "data", download_dir=tmp_path / "downloads",
                                database_url=f"sqlite+aiosqlite:///{tmp_path}/existing.db")
        assert custom.database_path == tmp_path / "existing.db"
        assert not custom.database_path.exists()


def test_healthcheck_accepts_waiting_for_config_but_rejects_wrong_bot() -> None:
    class Opener:
        def __init__(self, bot_id: str) -> None:
            self.bot_id = bot_id

        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8080/api/v1/health"
            assert request.get_header("Authorization") == "Bearer fixture-service"
            return io.BytesIO(json.dumps({"protocolVersion": 2, "botId": self.bot_id, "ready": False}).encode())

    with patch.dict("os.environ", {"BOT_TOKEN": "123:fixture", "WORKER_SERVICE_KEY": "fixture-service"}, clear=True):
        with patch("worker.healthcheck.build_opener", return_value=Opener("123")):
            assert main() == 0
        with patch("worker.healthcheck.build_opener", return_value=Opener("456")):
            assert main() == 1
        with patch("worker.healthcheck.build_opener", side_effect=OSError):
            assert main() == 1
