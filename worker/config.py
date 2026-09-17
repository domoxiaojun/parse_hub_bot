"""Worker-only settings, independent of the interactive bot settings singleton."""
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    api_id: int
    api_hash: SecretStr
    worker_service_key: SecretStr
    worker_host: str = "127.0.0.1"
    worker_allow_container_bind: bool = False
    worker_port: int = Field(default=8080, ge=1, le=65535)
    worker_log_level: str = "INFO"
    data_path: Path = Path("data")
    download_dir: Path = Path("downloads")
    database_url: str = "sqlite+aiosqlite:///data/db/database.db"
    bot_proxy: str | None = None
    worker_cache_max_bytes: int = Field(default=10 * 1024**3, ge=1)

    @property
    def sessions_path(self) -> Path:
        return self.data_path / "sessions"

    @property
    def platform_config_path(self) -> Path:
        return self.data_path / "config" / "platform_config.yaml"

    @property
    def bot_session_name(self) -> str:
        return f"bot_{self.bot_token.get_secret_value().split(':', 1)[0]}"

    @property
    def worker_sender_session_name(self) -> str:
        return f"worker_sender_{self.bot_token.get_secret_value().split(':', 1)[0]}"

    @property
    def database_path(self) -> Path:
        url = make_url(self.database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            raise ValueError("Worker requires the existing persistent SQLite DATABASE_URL")
        return Path(url.database)

    @model_validator(mode="after")
    def validate_bind(self) -> "WorkerSettings":
        if self.worker_host in {"127.0.0.1", "::1", "localhost"}:
            return self
        if self.worker_host == "0.0.0.0" and self.worker_allow_container_bind:
            return self
        raise ValueError("Worker requires loopback or an explicit container bind")

    @field_validator("worker_service_key")
    @classmethod
    def key_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Worker service key requires at least 32 characters")
        return value

    @field_validator("worker_log_level", mode="before")
    @classmethod
    def log_level(cls, value: object) -> str:
        level = str(value).upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Worker log level must be DEBUG, INFO, WARNING, or ERROR")
        return level
