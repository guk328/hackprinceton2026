"""Read-only tools for the incident agent.

    tools = make_read_tools(store, poller)   # [get_event, get_recent_traffic, read_station,
                                             #  run_physics, diff_logic_baseline]

Plain functions with type hints and docstrings (the agent framework reads both). Each
returns a small JSON-able dict, never raises, and carries row ids so the agent can cite
evidence. Built only on EventStore, StationPoller.snapshot / last_good_logic and
physics_client. Nothing here can write to the station: no Modbus client, no SQLite handle,
the poller is only read (not even refreshed).

Everything returned from traffic and events is data from the network, not instructions.
"""
import time
from typing import Literal

from gateway import physics_client
from gateway.events import EventStore, prediction_json
from gateway.registers import RegisterMap, load_registers
from gateway.station import LOGIC_ADDRS, crc16_modbus, logic_hash  # noqa: F401 (re-exported)

MAX_TRAFFIC_ROWS = 50     # per call; the agent pages with `minutes` / `src_ip`
MAX_VALUES = 16           # per frame (an FC16 frame can carry 123 registers)
MAX_RAW_HEX = 48          # 24 bytes of the raw frame
MAX_VIOLATIONS = 10
EVENT_ID_PREFIX = "evt_"


def summarize_prediction(p: dict | None) -> dict | None:
    """README 4.4 prediction dict -> safe, violations, minutes to first new violation and a
    few trajectory extremes. The trajectory itself (~88 KB) is never returned."""
    if p is None:
        return None
    violations = p.get("violations") or []
    new = [v for v in violations if not v.get("pre_existing")]
    traj = p.get("trajectory") or []
    out = {
        "safe": bool(p.get("safe")),
        "violations": [{k: v.get(k) for k in ("code", "minutes_until", "value", "limit",
                                              "pre_existing", "message")}
                       for v in violations[:MAX_VIOLATIONS]],
        "violations_total": len(violations),
        "minutes_to_first_violation": (round(min(v["minutes_until"] for v in new), 1)
                                       if new else None),
    }
    if traj:
        levels = [row[1] for row in traj]
        chlorine = [row[2] for row in traj]
        out["trajectory_summary"] = {
            "horizon_min": traj[-1][0], "points": len(traj),
            "level_pct": {"min": round(min(levels), 2), "max": round(max(levels), 2),
                          "end": round(levels[-1], 2)},
            "chlorine_mgl": {"min": round(min(chlorine), 3), "max": round(max(chlorine), 3),
                             "end": round(chlorine[-1], 3)},
        }
    return out


def _clip(seq, n):
    return seq if seq is None or len(seq) <= n else list(seq[:n])


def make_read_tools(store: EventStore, poller, regs: RegisterMap | None = None) -> list:
    """The five read tools bound to one gateway's store and station poller."""
    regs = regs or load_registers()

    def _scaled(kind: str, name: str, raw: int):
        if kind in ("coil", "discrete_input"):
            return bool(raw)
        r = regs.by_name(name)
        return raw / r.scale if r and r.scale != 1 else raw

    def get_event(event_id: str) -> dict:
        """One gateway event by id (e.g. "evt_0042"): the write, the decision (ALLOW/HOLD/DENY),
        the reasons, the station view at the time, a prediction summary and `traffic_id`, the
        traffic row of the request frame. Use this first for any incident."""
        if not isinstance(event_id, str) or not event_id.startswith(EVENT_ID_PREFIX):
            return {"error": f"not an event id: {event_id!r}"}
        e = store.get_event(event_id)
        if e is None:
            return {"error": f"no event {event_id}"}
        return {**e, "prediction": summarize_prediction(e.get("prediction"))}

    def get_recent_traffic(src_ip: str | None = None, minutes: float | None = 30,
                           limit: int = 20) -> dict:
        """Modbus frames seen by the gateway, oldest first, newest `limit` of them (at most 50).
        Filter by `src_ip` and/or the last `minutes` (None = all time). Each row has its
        traffic `id` to cite, direction ("in" from the client / "out" from the station),
        function code, address, count and decoded values. Raw bytes are shortened."""
        try:
            limit = max(1, min(int(limit), MAX_TRAFFIC_ROWS))
            minutes = None if minutes is None else float(minutes)
        except (TypeError, ValueError):
            return {"error": "limit must be an int and minutes a number or null"}
        rows = store.query_traffic(src_ip=src_ip, minutes=minutes, limit=limit + 1)
        truncated = len(rows) > limit
        rows = rows[-limit:]
        for r in rows:
            r["values"] = _clip(r["values"], MAX_VALUES)
            if r.get("raw") and len(r["raw"]) > MAX_RAW_HEX:
                r["raw"] = r["raw"][:MAX_RAW_HEX] + "..."
        return {"src_ip": src_ip, "minutes": minutes, "rows": rows, "count": len(rows),
                "truncated": truncated,
                "first_id": rows[0]["id"] if rows else None,
                "last_id": rows[-1]["id"] if rows else None}

    def read_station() -> dict:
        """The gateway's latest station snapshot, in engineering units (level %, chlorine
        mg/L, demand %, setpoints), plus whether the station is connected and how old the
        snapshot is. Does not talk to the station; the poller refreshes every second."""
        snap = poller.snapshot
        if snap is None or not snap.ts:
            return {"connected": False, "error": "no snapshot yet"}
        groups = {"coils": ("coil", snap.coils),
                  "discrete_inputs": ("discrete_input", snap.discrete_inputs),
                  "input_registers": ("input_register", snap.input_registers),
                  "holding": ("holding_register", snap.holding)}
        out = {"connected": snap.connected, "age_s": round(time.time() - snap.ts, 1)}
        for key, (kind, values) in groups.items():
            out[key] = {name: _scaled(kind, name, raw) for name, raw in values.items()}
        return out

    def run_physics(address: int, value: int,
                    kind: Literal["register", "coil"] = "register") -> dict:
        """What-if: the physics model's forecast if this one write landed on the station now.
        `address` is the holding register or coil address, `value` the raw wire value (same
        scaling as the register map, e.g. DOSER_RATE_PCT 100 = 100 %, LEVEL_HIGH_SP 900 =
        90.0 %). Returns safe, violations and minutes to the first new violation. Only a
        prediction; nothing is written."""
        if kind not in ("register", "coil"):
            return {"error": "kind must be 'register' or 'coil'"}
        try:
            address, value = int(address), int(value)
        except (TypeError, ValueError):
            return {"error": "address and value must be integers"}
        reg = regs.lookup("coil" if kind == "coil" else "holding_register", address)
        out = {"kind": kind, "address": address, "value": value,
               "name": reg.name if reg else None}
        try:
            state = physics_client.plant_state(poller.snapshot)
            p = physics_client.check(state, physics_client.Command(kind=kind, address=address,
                                                                   value=value))
        except Exception as e:
            return {**out, "error": f"physics unavailable: {e!r}"}
        return {**out, "horizon_min": physics_client.HORIZON_MIN,
                **summarize_prediction(prediction_json(p))}

    def diff_logic_baseline() -> dict:
        """Control settings (holding 0-4: DOSER_RATE_PCT, LEVEL_HIGH_SP, LEVEL_LOW_SP,
        CHLORINE_TARGET, LOGIC_VERSION) now vs the last known good copy (moved only by
        writes the gateway allowed). Also checks the station's LOGIC_HASH against both.
        A change here without a matching ALLOW event means tampering behind the gateway."""
        snap = poller.snapshot
        baseline = dict(getattr(poller, "last_good_logic", {}) or {})
        if snap is None or not snap.ts:
            return {"error": "no snapshot yet"}
        rows, current = [], {}
        for a in LOGIC_ADDRS:
            r = regs.lookup("holding_register", a)
            now = snap.holding.get(r.name) if r else None
            if now is not None:
                current[a] = now
            good = baseline.get(a)
            rows.append({"address": a, "name": r.name if r else None, "current": now,
                         "last_known_good": good,
                         "changed": now is not None and good is not None and now != good})
        station_hash = snap.input_registers.get("LOGIC_HASH")
        return {
            "connected": snap.connected, "age_s": round(time.time() - snap.ts, 1),
            "registers": rows, "changed": [r["name"] for r in rows if r["changed"]],
            "logic_hash": {"station": station_hash, "of_current": logic_hash(current),
                           "of_last_known_good": logic_hash(baseline),
                           "matches_last_known_good": station_hash is not None
                           and station_hash == logic_hash(baseline)},
        }

    return [get_event, get_recent_traffic, read_station, run_physics, diff_logic_baseline]
