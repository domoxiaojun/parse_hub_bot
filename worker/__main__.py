"""Run with uv run python -m worker. This entrypoint never starts bot.py."""
import asyncio
import fcntl
import logging
import signal

from aiohttp import web

from worker.app import create_app
from worker.config import WorkerSettings
from worker.jobs import Jobs
from worker.platform_config import PlatformConfigFile
from worker.sender import TelegramSender, create_sender_client
from worker.sender_runtime import SenderRuntime
from worker.store import Store


def configure_logging(level: str = "INFO") -> None:
    # Shared media helpers import the interactive bot logger, which replaces the
    # root handlers and sets ERROR at import time. Restore Worker logging after
    # loading the engine so progress and media failures remain visible. DEBUG is
    # scoped to our logger so dependency traces cannot disclose request details.
    root_level = "INFO" if level == "DEBUG" else level
    logging.basicConfig(level=root_level, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("parsehub.worker").setLevel(level)


async def main() -> None:
    # Import the parsing adapter only after Worker settings have been validated.
    settings = WorkerSettings()  # type: ignore[call-arg]
    from db.engine import close_db
    from db.init import init_db
    from services.parser import ParseService
    from worker.engine import ParseHubEngine
    configure_logging(settings.worker_log_level)

    sessions = settings.sessions_path
    sessions.mkdir(parents=True, exist_ok=True)
    lock = (sessions / f"{settings.bot_session_name}.worker.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("Another Worker owns this data directory") from None
    store = None
    runner = None
    sender_runtime = None
    try:
        await init_db()
        store = Store(settings.data_path, settings.worker_cache_max_bytes,
                      database_path=settings.database_path, files_path=settings.download_dir)
        expected_id = settings.bot_token.get_secret_value().split(":", 1)[0]
        parse_service = ParseService()
        engine = ParseHubEngine(store.files, parse_service)
        client = create_sender_client(settings)
        sender_runtime = SenderRuntime(
            client, expected_id, sessions / f"{settings.worker_sender_session_name}.cooldown.json",
        )
        jobs = Jobs(engine, store, expected_id, TelegramSender(client, sender_runtime))
        platform_config = PlatformConfigFile(settings.platform_config_path, engine.capabilities()["platforms"])
        jobs.configure(platform_config.active_config())
        runner = web.AppRunner(create_app(jobs, settings.worker_service_key.get_secret_value(), platform_config),
                               access_log=None)
        await runner.setup()
        await web.TCPSite(runner, settings.worker_host, settings.worker_port).start()
        logging.getLogger("parsehub.worker").info("event=http.ready sender_state=starting")
        sender_runtime.start()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        await stopped.wait()
    finally:
        if runner:
            await runner.cleanup()
        if sender_runtime:
            await sender_runtime.close()
        if store:
            store.close()
        await close_db()
        lock.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
