"""Event store: every decision and every frame, in SQLite, behind three calls.

    save_event(event, traffic_id=None) -> dict   README 4.4 event; assigns id and ts, publishes it
    get_event(event_id)                -> dict | None
    query_traffic(src_ip, minutes, limit) -> list[dict]   frames, oldest first

plus save_traffic() (one row per Modbus frame the proxy sees) and subscribe() (a
queue of new events per consumer: dashboard, agent runner). Agent activity
(save_agent_step) goes to the same subscribers as dicts with "_kind": "agent";
events have no "_kind". The spend table is the model-cost ledger (agent/budget.py).

DATABASE_URL picks the backend. Only sqlite:/// is implemented; any other scheme
raises, so a Postgres URL is never silently ignored. To move to Postgres/Tiger
Data later, reimplement this class with the same methods.

Thread-safe: one connection under a lock, so the proxy (event loop) and agent
tools (worker threads) can share it. Subscribers are fed with
loop.call_soon_threadsafe, so save_event may be called from any thread.

Demo-grade, not utility-grade: the SQLite file is plain, unsigned and
unencrypted (an attacker with host access can edit the audit trail), traffic is
never pruned, and a database write blocks the event loop for a moment.
"""
import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_URL = "sqlite:///backflow.db"
QUEUE_SIZE = 1000   # per subscriber; a stalled consumer loses its oldest events, not the proxy

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT UNIQUE,
    ts         TEXT NOT NULL,
    src_ip     TEXT,
    function   INTEGER,
    kind       TEXT,
    address    INTEGER,
    name       TEXT,
    old_value  INTEGER,
    new_value  INTEGER,
    decision   TEXT NOT NULL,
    reasons    TEXT NOT NULL,
    prediction TEXT,
    station    TEXT,
    traffic_id INTEGER
);
CREATE INDEX IF NOT EXISTS events_src ON events (src_ip, seq);
CREATE TABLE IF NOT EXISTS traffic (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    src_ip    TEXT,
    direction TEXT NOT NULL,
    tid       INTEGER,
    fc        INTEGER,
    address   INTEGER,
    count     INTEGER,
    vals      TEXT,
    raw       TEXT
);
CREATE INDEX IF NOT EXISTS traffic_src ON traffic (src_ip, id);
CREATE TABLE IF NOT EXISTS agent_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    event_id TEXT,
    kind     TEXT NOT NULL,
    text     TEXT,
    data     TEXT
);
CREATE TABLE IF NOT EXISTS spend (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    event_id      TEXT,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);
"""

_EVENT_FIELDS = ("src_ip", "function", "kind", "address", "name", "old_value",
                 "new_value", "decision", "reasons", "prediction", "station")


def utc_now(ms: bool = False) -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z" if ms \
        else t.strftime("%Y-%m-%dT%H:%M:%SZ")


def sqlite_path(url: str) -> str:
    """'sqlite:///backflow.db' -> 'backflow.db'; 'sqlite:////tmp/x.db' -> '/tmp/x.db'."""
    u = urlparse(url)
    if u.scheme != "sqlite":
        raise ValueError(f"DATABASE_URL {url!r}: only sqlite:/// is implemented")
    path = u.path[1:] if u.path.startswith("/") else u.path
    return path or ":memory:"


def jsonable(obj):
    """Dataclasses, tuples and plain values -> something json.dumps accepts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    return obj


def prediction_json(p) -> dict | None:
    """The README 4.4 'prediction' object from a physics Prediction (or None)."""
    if p is None:
        return None
    return {"safe": p.safe, "violations": jsonable(p.violations),
            "trajectory": jsonable(p.trajectory)}


class EventStore:
    def __init__(self, url: str = DEFAULT_URL):
        self.url = url
        self._db = sqlite3.connect(sqlite_path(url), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._subs: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")   # no-op for :memory:
            self._db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    def clear(self):
        """Demo reset: drop events, traffic and agent steps and restart their ids. The spend
        ledger is kept."""
        with self._lock:
            for table in ("events", "traffic", "agent_log"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("DELETE FROM sqlite_sequence WHERE name IN ('events','traffic','agent_log')")
            self._db.commit()

    # ---------------------------------------------------------------- traffic

    def save_traffic(self, src_ip, direction, tid, fc, address=None, count=None,
                     values=None, raw: bytes | None = None) -> int:
        """One Modbus frame. Returns the row id, which events cite as evidence."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO traffic (ts, src_ip, direction, tid, fc, address, count, vals, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (utc_now(ms=True), src_ip, direction, tid, fc, address, count,
                 json.dumps(values), raw.hex() if raw is not None else None))
            self._db.commit()
            return cur.lastrowid

    def query_traffic(self, src_ip: str | None = None, minutes: float | None = None,
                      limit: int = 200) -> list[dict]:
        """The newest `limit` frames (optionally one source, optionally the last
        `minutes`), returned oldest first."""
        where, args = [], []
        if src_ip is not None:
            where.append("src_ip = ?")
            args.append(src_ip)
        if minutes is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            where.append("ts >= ?")
            args.append(cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        sql = "SELECT * FROM traffic" + (" WHERE " + " AND ".join(where) if where else "")
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "src_ip": r["src_ip"], "direction": r["direction"],
                 "tid": r["tid"], "fc": r["fc"], "address": r["address"], "count": r["count"],
                 "values": json.loads(r["vals"]), "raw": r["raw"]} for r in reversed(rows)]

    # ----------------------------------------------------------------- events

    def save_event(self, event: dict, traffic_id: int | None = None) -> dict:
        """Store a README 4.4 event (without id; `ts` defaults to now). Returns it
        with its new `id` ("evt_0042") and `traffic_id` (the request frame row it
        came from), and offers it to every subscriber."""
        event = {**event, "ts": event.get("ts") or utc_now()}
        row = [event.get(k) for k in _EVENT_FIELDS]
        row[_EVENT_FIELDS.index("reasons")] = json.dumps(event.get("reasons") or [])
        for k in ("prediction", "station"):
            i = _EVENT_FIELDS.index(k)
            row[i] = None if row[i] is None else json.dumps(row[i])
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (ts, " + ", ".join(_EVENT_FIELDS) + ", traffic_id)"
                " VALUES (?" + ",?" * (len(_EVENT_FIELDS) + 1) + ")",
                (event["ts"], *row, traffic_id))
            event_id = f"evt_{cur.lastrowid:04d}"
            self._db.execute("UPDATE events SET id = ? WHERE seq = ?", (event_id, cur.lastrowid))
            self._db.commit()
        saved = {"id": event_id, **event, "traffic_id": traffic_id}
        self._publish(saved)
        return saved

    def get_event(self, event_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if r is None:
            return None
        out = {"id": r["id"], "ts": r["ts"]}
        for k in _EVENT_FIELDS:
            out[k] = r[k]
        out["reasons"] = json.loads(r["reasons"])
        for k in ("prediction", "station"):
            out[k] = None if r[k] is None else json.loads(r[k])
        out["traffic_id"] = r["traffic_id"]
        return out

    def list_events(self, limit: int = 100) -> list[dict]:
        """Newest first. Used by the API in a later step; the agent uses get_event."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM events ORDER BY seq DESC LIMIT ?", (limit,))]
        return [self.get_event(i) for i in ids]

    # ------------------------------------------------------------ agent steps

    def save_agent_step(self, event_id: str | None, kind: str, text: str = "",
                        data: dict | None = None) -> dict:
        """One line of agent activity (tool call, note, verdict, operator text...). Stored for
        page reloads and offered to subscribers as a message with "_kind": "agent"."""
        ts = utc_now(ms=True)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO agent_log (ts, event_id, kind, text, data) VALUES (?,?,?,?,?)",
                (ts, event_id, kind, text, None if data is None else json.dumps(jsonable(data))))
            self._db.commit()
        step = {"_kind": "agent", "id": cur.lastrowid, "ts": ts, "event_id": event_id,
                "kind": kind, "text": text, "data": data}
        self._publish(step)
        return step

    def list_agent_steps(self, limit: int = 200, event_id: str | None = None) -> list[dict]:
        """The newest `limit` steps, oldest first."""
        sql, args = "SELECT * FROM agent_log", []
        if event_id is not None:
            sql, args = sql + " WHERE event_id = ?", [event_id]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "event_id": r["event_id"], "kind": r["kind"],
                 "text": r["text"], "data": None if r["data"] is None else json.loads(r["data"])}
                for r in reversed(rows)]

    # ------------------------------------------------------------------ spend

    def add_spend(self, event_id, input_tokens: int, output_tokens: int, cost_usd: float):
        with self._lock:
            self._db.execute(
                "INSERT INTO spend (ts, event_id, input_tokens, output_tokens, cost_usd)"
                " VALUES (?,?,?,?,?)", (utc_now(), event_id, input_tokens, output_tokens, cost_usd))
            self._db.commit()

    def total_spend(self) -> float:
        with self._lock:
            return self._db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend").fetchone()[0]

    # ------------------------------------------------------------------ queue

    def subscribe(self, maxsize: int = QUEUE_SIZE) -> asyncio.Queue:
        """A queue that receives every event saved from now on. Call it from the
        consumer's own event loop. Each subscriber gets its own copy of the stream."""
        q: asyncio.Queue = asyncio.Queue(maxsize)
        self._subs[q] = asyncio.get_running_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.pop(q, None)

    def _publish(self, event: dict) -> None:
        for q, loop in list(self._subs.items()):
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:           # that consumer's loop is closed
                self._subs.pop(q, None)

    @staticmethod
    def _offer(q: asyncio.Queue, event: dict) -> None:
        if q.full():
            q.get_nowait()
            log.warning("event queue full, dropped the oldest event")
        q.put_nowait(event)


def open_store(url: str | None = None) -> EventStore:
    """The store for DATABASE_URL (from the environment unless given)."""
    return EventStore(url or os.getenv("DATABASE_URL") or DEFAULT_URL)
