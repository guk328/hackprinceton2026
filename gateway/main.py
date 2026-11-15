"""The whole gateway in one process: proxy (:GATEWAY_PORT), station poller, API (:API_PORT)
and the incident runner (agent, operator channel).

Run: python -m gateway.main   (or: python tasks.py gateway)
"""
import asyncio
import logging
import os

import uvicorn

from gateway import events
from gateway.agent.budget import Budget
from gateway.agent.runner import Runner
from gateway.agent.tools_act import make_action_tools, release
from gateway.agent.tools_read import make_read_tools
from gateway.api import REPORTS_DIR, create_app
from gateway.auth import Auth
from gateway.blocklist import Blocklist
from gateway.held import HeldStore
from gateway.notify import OperatorChannel
from gateway.proxy import Proxy, config
from gateway.reset import reset
from gateway.station import StationPoller
from gateway.station_write import StationWriter

log = logging.getLogger("gateway.main")


def build(cfg: dict, store, loop) -> dict:
    """Wire the parts. The agent is optional: without AGENT_MODEL (or if it cannot be
    built) the runner still texts the operator with a templated message."""
    poller = StationPoller(cfg["station_host"], cfg["station_port"])
    blocklist = Blocklist()
    writer = StationWriter(cfg["station_host"], cfg["station_port"])
    proxy = Proxy(**cfg, poller=poller, store=store, blocklist=blocklist,
                  held=HeldStore(store=store, regs=poller.regs),
                  alarm=writer.alarm if os.getenv("ALARM_ON_HOLD", "1") != "0" else None)
    channel = OperatorChannel(store)
    budget = Budget(store)

    def refresh():   # called from agent worker threads
        return asyncio.run_coroutine_threadsafe(poller.refresh(), loop).result(5)

    kit = dict(held=proxy.held, channel=channel, writer=writer, refresh=refresh,
               store=store, poller=poller, lock=proxy.run_locked)

    def make_agent():
        from gateway.agent.build import INTERRUPT_ON, build_agent
        tools = make_read_tools(store, poller) + make_action_tools(
            store, poller, blocklist, channel, writer, held=proxy.held, refresh=refresh, lock=proxy.run_locked)
        from gateway.agent.reports import make_save_report
        from gateway.agent.subagents import make_subagents
        subs = make_subagents(make_read_tools(store, poller), make_save_report(store, REPORTS_DIR))
        return build_agent(tools, budget=budget, interrupt_on=INTERRUPT_ON, subagents=subs)

    runner = Runner(store, channel, blocklist, budget=budget, held=proxy.held,
                    make_agent=make_agent if os.getenv("AGENT_MODEL") else None,
                    release=lambda event_id: release(event_id, **kit), reports_dir=REPORTS_DIR,
                    revert=lambda: writer.restore_logic(poller.last_good_logic))
    def do_reset():
        return reset(store, blocklist=blocklist, held=proxy.held, channel=channel, runner=runner,
                     poller=poller, writer=writer, reports_dir=REPORTS_DIR)

    return dict(poller=poller, proxy=proxy, channel=channel, runner=runner, budget=budget,
                blocklist=blocklist, reset=do_reset)


async def run(cfg: dict, api_host: str, api_port: int):
    store = events.open_store()
    g = build(cfg, store, asyncio.get_running_loop())
    await g["poller"].start()
    await g["proxy"].start()
    await g["runner"].start()
    if os.getenv("BACKFLOW_RESET_ON_START") == "1":
        await asyncio.to_thread(g["reset"])
    log.info("agent model: %s, spend so far $%.4f of $%.2f", os.getenv("AGENT_MODEL") or "none",
             g["budget"].spent(), g["budget"].ceiling)
    app = create_app(store, g["poller"], g["proxy"]._station_view, channel=g["channel"], auth=Auth(),
                     reset=g["reset"])
    server = uvicorn.Server(uvicorn.Config(app, host=api_host, port=api_port, log_level="info"))
    try:
        await server.serve()
    finally:
        await g["runner"].close()
        await g["proxy"].close()
        await g["poller"].close()
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
