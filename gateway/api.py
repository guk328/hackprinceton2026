"""HTTP API for the dashboard and the operator's phone panel.

    GET  /events            newest first, ?limit=100; trajectory omitted (it is ~88 KB)
    GET  /events/{id}       one event, with the full prediction trajectory
    GET  /state             last station snapshot (raw registers + README 4.4 `station` view)
    GET  /stream            server-sent events: "event" for every new event (no trajectory),
                            "agent" for every agent step and operator text
    GET  /agent/log         stored agent steps, oldest first, ?limit=200
    POST /operator/reply    {"reply": "ALLOW"|"BLOCK", "event_id": "evt_0042"} -> inbox queue
    GET  /reports/{id}      gateway/reports/<id>.md as text
    GET  /                  dashboard/ (static, mounted last so the routes above win)

Read-only except /operator/reply, which only queues an answer for the agent runner
(a later step); nothing here writes to the station or touches the allow/hold path.
"""
import asyncio
import dataclasses
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from gateway.events import EventStore

REPORTS_DIR = Path(__file__).parent / "reports"
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
EVENT_ID = re.compile(r"^evt_\d{4,}$")


class OperatorReply(BaseModel):
    reply: Literal["ALLOW", "BLOCK"]
    event_id: str | None = None


def slim(event: dict) -> dict:
    """Event without the trajectory; `prediction` keeps safe + violations."""
    p = event.get("prediction")
    if p and "trajectory" in p:
        event = {**event, "prediction": {k: v for k, v in p.items() if k != "trajectory"}}
    return event


def create_app(store: EventStore, poller=None, station_view=None,
               reports_dir: Path = REPORTS_DIR, dashboard_dir: Path = DASHBOARD_DIR) -> FastAPI:
    """`poller` is a StationPoller (or None: /state reports not connected);
    `station_view` turns its snapshot into the README 4.4 station object."""
    app = FastAPI(title="Backflow gateway")
    app.state.operator_replies = asyncio.Queue()   # drained by the agent runner (step 21+)

    @app.get("/events")
    def list_events(limit: int = Query(100, ge=1, le=1000)):
        return [slim(e) for e in store.list_events(limit)]

    @app.get("/events/{event_id}")
    def get_event(event_id: str):
        event = store.get_event(event_id)
        if event is None:
            raise HTTPException(404, f"no event {event_id}")
        return event

    @app.get("/state")
    def state():
        snap = poller.snapshot if poller else None
        if snap is None:
            return {"connected": False}
        out = dataclasses.asdict(snap)
        if station_view:
            out["station"] = station_view(snap)
        return out

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream():
        q = store.subscribe()
        try:
            while True:
                item = await q.get()
                if "_kind" in item:          # agent activity, operator texts
                    yield ServerSentEvent(data=item, event=item["_kind"])
                else:
                    yield ServerSentEvent(data=slim(item), event="event", id=item["id"])
        finally:
            store.unsubscribe(q)

    @app.get("/agent/log")
    def agent_log(limit: int = Query(200, ge=1, le=1000)):
        return store.list_agent_steps(limit)

    @app.post("/operator/reply", status_code=202)
    async def operator_reply(body: OperatorReply):
        if body.event_id is not None and store.get_event(body.event_id) is None:
            raise HTTPException(404, f"no event {body.event_id}")
        await app.state.operator_replies.put(body.model_dump())
        return {"accepted": True}

    @app.get("/reports/{event_id}", response_class=PlainTextResponse)
    def report(event_id: str):
        if not EVENT_ID.match(event_id):       # no path tricks
            raise HTTPException(404, "no such report")
        path = reports_dir / f"{event_id}.md"
        if not path.is_file():
            raise HTTPException(404, f"no report for {event_id}")
        return path.read_text(encoding="utf-8")

    if dashboard_dir.is_dir():
        app.mount("/", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")
    return app
