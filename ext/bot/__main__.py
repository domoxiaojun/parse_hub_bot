"""Run with ``uv run python -m ext.bot``; never run ``bot.py`` directly for the fork."""

from ext import hooks

# Mask proxy credentials before ``core`` logs the platform configuration at import time.
hooks.install_log_patcher()

from ext.bot.app import create_bot  # noqa: E402

if __name__ == "__main__":
    create_bot().run()
