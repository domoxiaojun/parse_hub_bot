"""Interactive Bot entry point for the fork: upstream ``Bot`` plus fork hooks and handlers."""

from typing import Any

from bot import Bot
from ext import hooks
from ext.bot import inline
from ext.bot.registry import EXCLUDED_UPSTREAM_PLUGINS, register_module_handlers
from log import logger

logger = logger.bind(name="ForkBot")


class ForkBot(Bot):
    def __init__(self) -> None:
        super().__init__()
        self.plugins = {"root": "plugins", "exclude": list(EXCLUDED_UPSTREAM_PLUGINS)}
        registered = register_module_handlers(self, inline)
        logger.debug(f"已注册 Fork inline handler: {registered}")

    async def stop(self, *args: Any, **kwargs: Any) -> Any:
        # Per-request pipelines clean their own outputs. DOWNLOAD_DIR is shared with
        # the Worker cache, so the upstream shutdown rmtree must be skipped.
        self.cfg.debug_skip_cleanup = True
        return await super().stop(*args, **kwargs)


def create_bot() -> ForkBot:
    hooks.install(bot=True)
    return ForkBot()
