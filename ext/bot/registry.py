"""Handler registration helpers that do not import ``bot`` (no event-loop or log-sink side effects)."""

from typing import Any

# Upstream plugin modules replaced wholesale by fork modules; kurigram unloads them via ``plugins["exclude"]``.
EXCLUDED_UPSTREAM_PLUGINS: tuple[str, ...] = ("parse.inline",)


def handler_names(module: Any) -> list[str]:
    """Names of every kurigram-decorated handler function in ``module``."""
    return [name for name in vars(module) if isinstance(getattr(getattr(module, name), "handlers", None), list | tuple)]


def register_module_handlers(client: Any, module: Any) -> int:
    """Register every decorated handler function of ``module`` the way kurigram's plugin loader does."""
    count = 0
    for name in handler_names(module):
        for handler, group in getattr(module, name).handlers:
            client.add_handler(handler, group)
            count += 1
    return count
