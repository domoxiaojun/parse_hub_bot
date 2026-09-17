"""Authenticated container liveness check without importing the Telegram client."""
import json
import os
from urllib.request import ProxyHandler, Request, build_opener


def main() -> int:
    try:
        secret = os.environ["WORKER_SERVICE_KEY"]
        bot_id = os.environ["BOT_TOKEN"].split(":", 1)[0]
        port = int(os.environ.get("WORKER_PORT", "8080"))
        request = Request(f"http://127.0.0.1:{port}/api/v1/health",
                          headers={"Authorization": f"Bearer {secret}"})
        # Environment proxies must never receive the local service credential.
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            data = json.loads(response.read(16384))
        # Configuration/sender readiness is separate from liveness, including Telegram FloodWait.
        return 0 if data.get("protocolVersion") == 3 and data.get("botId") == bot_id else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
