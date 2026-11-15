"""Incident runner: turns HOLD/DENY events into agent threads, and operator replies into
their continuation. Runs on the gateway's event loop; the agent itself runs in worker threads.

    event (HOLD/DENY/tamper) -> one incident per source per DEBOUNCE_S -> agent investigates,
    acts, texts the operator -> verified reply -> BLOCK: source blocked by plain code, agent
    told; ALLOW: agent may ask for release, which re-runs the safety checks.

The operator is always told. If there is no model, the model fails, the run passes
TIME_LIMIT_S, or the spend ceiling is reached, a templated text goes out instead, and the
reply is then handled by plain code alone. None of this is in the allow/hold path.
"""
import asyncio
import logging
import time

from gateway.agent.budget import BudgetExceeded
from gateway.agent.build import Session

log = logging.getLogger(__name__)

DEBOUNCE_S = 15.0        # further events from the same source join the open incident
TIME_LIMIT_S = 120.0     # per agent run
BLOCK_MINUTES = 60
MAX_SETTLE = 3
TRIGGERS = ("HOLD", "DENY")


def template_text(event: dict) -> str:
    """The text the operator gets when the agent cannot write one."""
    what = event.get("name") or f"{event.get('kind') or 'function'} {event.get('address') or event.get('function')}"
    change = "" if event.get("new_value") is None else (
        f" {event['old_value']} -> {event['new_value']}" if event.get("old_value") is not None
        else f" = {event['new_value']}")
    why = (event.get("reasons") or ["no reason recorded"])[0]
    for v in (event.get("prediction") or {}).get("violations") or []:
        if not v.get("pre_existing") and v.get("message"):   # the model's plain-English line
            why = v["message"]
            break
    verb = "held" if event.get("decision") == "HOLD" else "refused"
    text = (f"Backflow {verb} a command from {event.get('src_ip')}: {what}{change}. "
            f"Why: {why}. It did not reach the station. Reply BLOCK or ALLOW")
    return text if len(text) <= 320 else text[:296].rstrip() + "... Reply BLOCK or ALLOW"


class Runner:
    def __init__(self, store, channel, blocklist, *, make_agent=None, budget=None, release=None,
                 held=None, debounce_s: float = DEBOUNCE_S, time_limit_s: float = TIME_LIMIT_S):
        """`make_agent()` builds the agent (called once, lazily, in a worker thread; None or a
        failure means templated texts only). `release(event_id)` is the plain-code release
        used when there is no agent thread to do it."""
        self.store, self.channel, self.blocklist = store, channel, blocklist
        self.make_agent, self.budget, self.release, self.held = make_agent, budget, release, held
        self.debounce_s, self.time_limit_s = debounce_s, time_limit_s
        self.sessions: dict[str, Session] = {}
        self._agent = None
        self._agent_failed = make_agent is None
        self._recent: dict[str, tuple[float, str]] = {}   # src_ip -> (time, incident event id)
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task | None = None

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.channel.on_reply = self._reply_from_any_thread
        q = self.store.subscribe()
        self._main = asyncio.create_task(self._consume(q))

    async def close(self):
        for t in [self._main, *self._tasks]:
            if t:
                t.cancel()
        await asyncio.gather(*[t for t in [self._main, *self._tasks] if t], return_exceptions=True)

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _step(self, event_id, kind, text, data=None):
        try:
            self.store.save_agent_step(event_id, kind, text, data)
        except Exception:
            log.exception("could not store an agent step")

    async def _consume(self, q):
        try:
            while True:
                item = await q.get()
                if "_kind" in item or item.get("decision") not in TRIGGERS:
                    continue
                src, now = item.get("src_ip") or "?", time.monotonic()
                last = self._recent.get(src)
                if last and now - last[0] < self.debounce_s:
                    self._step(last[1], "status", f"{item['id']} from the same source joins this incident",
                               {"event_id": item["id"]})
                    continue
                self._recent[src] = (now, item["id"])
                self._spawn(self._incident(item))
        finally:
            self.store.unsubscribe(q)

    # ------------------------------------------------------------- one incident

    def _get_agent(self):
        if self._agent is None and not self._agent_failed:
            try:
                self._agent = self.make_agent()
            except Exception as e:
                self._agent_failed = True
                log.warning("agent unavailable, templated texts only: %r", e)
        return self._agent

    def _run(self, fn, event_id, *args):
        """In a worker thread: one agent run, counted against the budget."""
        if self.budget is not None:
            if self.budget.exhausted():
                raise BudgetExceeded(f"spend ceiling ${self.budget.ceiling:.2f} reached")
            self.budget.start_run(event_id)
        return fn(*args)

    async def _incident(self, event: dict):
        eid = event["id"]
        self._step(eid, "status", f"Incident opened for {eid} ({event.get('decision')})")
        why = None
        try:
            agent = await asyncio.to_thread(self._get_agent)
            if agent is None:
                why = "no model configured"
            else:
                session = Session(agent, eid, lambda k, t, d=None: self._step(eid, k, t, d))
                self.sessions[eid] = session
                state = await asyncio.wait_for(
                    asyncio.to_thread(self._run, session.start, eid, event), self.time_limit_s)
                await self._settle(session, eid, state)
        except asyncio.TimeoutError:
            why = f"agent passed the {self.time_limit_s:g} s time limit"
        except BudgetExceeded as e:
            why = str(e)
        except Exception as e:
            log.exception("agent run failed")
            why = f"agent failed: {type(e).__name__}"
        if why:
            self.sessions.pop(eid, None)       # replies are handled by plain code from here
            self._step(eid, "status", f"Agent not used: {why}. Sending the standard text.")
        if not any(m["event_id"] == eid for m in self.channel.inbox()):
            self.channel.send(eid, template_text(event))
        self._cost(eid)

    def _cost(self, eid):
        if self.budget is not None and self.budget.calls:
            self._step(eid, "cost", self.budget.summary())

    async def _settle(self, session: Session, eid: str, state: str):
        """Answer a pause from the operator's verified reply, if there is one yet."""
        for _ in range(MAX_SETTLE):
            if state != "waiting":
                return
            decision = self.channel.decision(eid)
            if decision is None:
                return                          # stays paused until the operator answers
            state = await asyncio.wait_for(asyncio.to_thread(
                self._run, session.resume, eid, decision == "ALLOW",
                "The operator replied BLOCK. Do not release it."), self.time_limit_s)

    # ------------------------------------------------------------------ replies

    def _reply_from_any_thread(self, event_id: str, reply: str):
        self._loop.call_soon_threadsafe(lambda: self._spawn(self._on_reply(event_id, reply)))

    async def _on_reply(self, eid: str, reply: str):
        event = self.store.get_event(eid) or {}
        if reply == "BLOCK":                    # the operator's order: plain code, no model
            src = event.get("src_ip")
            try:
                self.blocklist.block(src, BLOCK_MINUTES)
                self._step(eid, "action", f"Source {src} blocked for {BLOCK_MINUTES} min (operator said BLOCK)")
            except (ValueError, TypeError):
                self._step(eid, "action", f"Could not block source {src!r}")
            cmd = self.held.by_event(eid) if self.held is not None else None
            if cmd is not None:
                self.held.pop(cmd.id)
                self._step(eid, "action", "Held command discarded")
        session = self.sessions.get(eid)
        try:
            if session is None:
                if reply == "ALLOW" and self.release is not None:
                    out = await asyncio.to_thread(self.release, eid)
                    if not out.get("released"):
                        self._step(eid, "status", f"Not released: {out.get('error')}")
                return
            if session.pending:
                await self._settle(session, eid, "waiting")
            else:
                note = ("The operator replied ALLOW (verified). If you judge the command legitimate, "
                        "call release_held_command; the gateway re-checks safety first."
                        if reply == "ALLOW" else
                        "The operator replied BLOCK (verified). The gateway has blocked the source "
                        "and discarded the held command.")
                state = await asyncio.wait_for(asyncio.to_thread(
                    self._run, session.tell, eid, note + " Then give your final verdict."),
                    self.time_limit_s)
                await self._settle(session, eid, state)
        except Exception as e:
            log.exception("agent could not process the reply")
            self._step(eid, "status", f"Agent could not process the reply: {type(e).__name__}")
            if reply == "ALLOW" and self.release is not None and \
                    self.held is not None and self.held.by_event(eid) is not None:
                out = await asyncio.to_thread(self.release, eid)
                if not out.get("released"):
                    self._step(eid, "status", f"Not released: {out.get('error')}")
        self._cost(eid)
