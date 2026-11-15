"""Debug entry: run the agent on one stored event with the read tools only.

    python -m gateway.agent.cli evt_0042

Uses AGENT_MODEL and the provider key from .env. This makes real, paid model calls; the
tokens and cost are printed at the end. The demo uses the dashboard, not this.
"""
import asyncio
import sys

from dotenv import load_dotenv


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    load_dotenv()
    from gateway import events
    from gateway.agent.budget import Budget, BudgetExceeded
    from gateway.agent.build import Session, build_agent
    from gateway.agent.tools_read import make_read_tools
    from gateway.proxy import config
    from gateway.station import StationPoller

    store = events.open_store()
    event = store.get_event(argv[0])
    if event is None:
        print(f"no event {argv[0]}")
        return 1
    cfg = config()

    async def snapshot_once():         # one snapshot; stays "offline" if the station is down
        p = StationPoller(cfg["station_host"], cfg["station_port"])
        await p.refresh()
        await p.close()
        return p

    poller = asyncio.run(snapshot_once())
    budget = Budget(store)
    budget.start_run(event["id"])

    def show(kind, text, data=None):
        store.save_agent_step(event["id"], kind, text, data)
        print(f"[{kind}] {text}")

    session = Session(build_agent(make_read_tools(store, poller), budget=budget), event["id"], show)
    try:
        session.start(event)
    except BudgetExceeded as e:
        print(f"stopped: {e}")
    finally:
        print(f"-- {budget.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
