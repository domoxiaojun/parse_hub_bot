from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

import db.models  # noqa: F401
from alembic import command
from db.base import Base
from db.session import engine


def _alembic_config(connection: Connection) -> Config:
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.attributes["connection"] = connection
    return alembic_cfg


def upgrade_head(connection: Connection) -> None:
    command.upgrade(_alembic_config(connection), "head")


def stamp_head(connection: Connection) -> None:
    command.stamp(_alembic_config(connection), "head")


def init_or_upgrade(connection: Connection) -> None:
    """Fresh databases get the current schema and are stamped; existing ones run pending migrations.

    Running create_all before upgrade on an existing database would make any future
    add_column/create_table migration collide with tables the ORM already created.
    """
    inspector = inspect(connection)
    fresh = not inspector.has_table("alembic_version") and not inspector.has_table("users")
    if fresh:
        Base.metadata.create_all(connection)
        stamp_head(connection)
        return
    if not inspector.has_table("alembic_version"):
        # Pre-alembic database: tables exist but were never stamped; keep the historical behaviour.
        Base.metadata.create_all(connection)
    upgrade_head(connection)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(init_or_upgrade)
