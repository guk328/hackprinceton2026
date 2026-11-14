"""The whole gateway in one process: proxy (:GATEWAY_PORT), station poller and API (:API_PORT).

Run: python -m gateway.main   (or: python tasks.py gateway)
"""
import asyncio
import logging
import os

import uvicorn

from gateway import events
from gateway.api import create_app
from gateway.proxy import Proxy, config
from gateway.station import StationPoller


async def run(cfg: dict, api_host: str, api_port: int):
    store = events.open_store()
    poller = StationPoller(cfg["station_host"], cfg["station_port"])
    await poller.start()
    proxy = Proxy(**cfg, poller=poller, store=store)
    await proxy.start()
    app = create_app(store, poller, proxy._station_view)
    server = uvicorn.Server(uvicorn.Config(app, host=api_host, port=api_port, log_level="info"))
    try:
        await server.serve()
    finally:
        await proxy.close()
        await poller.close()
        store.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config()   # loads .env first
    try:
        asyncio.run(run(cfg, os.getenv("API_HOST", "127.0.0.1"), int(os.getenv("API_PORT", "8000"))))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
