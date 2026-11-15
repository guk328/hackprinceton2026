"""HTTP API for the dashboard and the operator's phone panel.

    GET  /events            newest first, ?limit=100; trajectory omitted (it is ~88 KB)
    GET  /events/{id}       one event, with the full prediction trajectory
    GET  /state             last station snapshot (raw registers + README 4.4 `station` view)
    GET  /stream            server-sent events: "event" for every new event (no trajectory),
                            "agent" for every agent step and operator text
    GET  /agent/log         stored agent steps, oldest first, ?limit=200
    POST /login             {"password": "..."} -> admin cookie (gateway/auth.py); /logout; GET /me
    GET  /operator/inbox    admin: the operator's "phone", texts with their one-time codes
    POST /operator/reply    admin: {"reply": "ALLOW"|"BLOCK", "event_id": "evt_0042",
                            "code": "123456"}; 202 if the code verifies (gateway/notify.py), else 403

Every request body is a strict pydantic model (unknown fields, wrong types and odd ids are
refused with 422 before any code runs); ids in paths are pattern-checked.
    GET  /reports/{id}      gateway/reports/<id>.md as text
    GET  /                  dashboard/ (static, mounted last so the routes above win)

Read-only except /operator/reply, which hands a verified answer to the operator channel;
nothing here writes to the station or touches the allow/hold path.
"""
import dataclasses
import re
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth import COOKIE, Auth
from gateway.events import EventStore

REPORTS_DIR = Path(__file__).parent / "reports"
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
EVENT_ID_PATTERN = r"^evt_\d{4,12}$"
EVENT_ID = re.compile(EVENT_ID_PATTERN)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Strict):
    password: str = Field(min_length=1, max_length=200)


class OperatorReply(Strict):
    reply: Literal["ALLOW", "BLOCK"]
    event_id: str = Field(pattern=EVENT_ID_PATTERN)
    code: str = Field(pattern=r"^\d{6}$")


def slim(event: dict) -> dict:
    """Event without the trajectory; `prediction` keeps safe + violations."""
    p = event.get("prediction")
    if p and "trajectory" in p:
        event = {**event, "prediction": {k: v for k, v in p.items() if k != "trajectory"}}
    return event


def create_app(store: EventStore, poller=None, station_view=None, channel=None,
               auth: Auth | None = None, reports_dir: Path = REPORTS_DIR, dashboard_dir: Path = DASHBOARD_DIR) -> FastAPI:
    """`poller` is a StationPoller (or None: /state reports not connected);
    `station_view` turns its snapshot into the README 4.4 station object."""
    app = FastAPI(title="Backflow gateway")
    app.state.channel = channel
    auth = app.state.auth = auth or Auth()
    admin = [Depends(auth.require)]

    @app.post("/login")
    def login(body: Login, request: Request, response: Response):
        auth.login(body.password, request.client.host if request.client else "?", response)
        return {"admin": True}

    @app.post("/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"admin": False}

    @app.get("/me")
    def me(request: Request):
        return {"admin": auth.is_admin(request)}

    @app.get("/events")
    def list_events(limit: int = Query(100, ge=1, le=1000)):
        return [slim(e) for e in store.list_events(limit)]

    @app.get("/events/{event_id}")
    def get_event(event_id: str = PathParam(pattern=EVENT_ID_PATTERN)):
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

    @app.post("/operator/reply", status_code=202, dependencies=admin)
    def operator_reply(body: OperatorReply):
        if channel is None:
            raise HTTPException(503, "no operator channel")
        ok, why = channel.reply(body.event_id, body.reply, body.code)
        if not ok:
            raise HTTPException(403, why)
        return {"accepted": True}

    @app.get("/operator/inbox", dependencies=admin)
    def operator_inbox():
        return channel.inbox() if channel is not None else []

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
