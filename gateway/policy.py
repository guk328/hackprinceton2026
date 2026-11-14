"""decide(): the allow/hold/deny rule for one write. Deterministic, no LLM, fails closed.

Every check runs and every reason is kept; the strictest verdict wins (DENY > HOLD > ALLOW).
"""
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime

from gateway import physics_client
from gateway.frames import WriteRequest
from gateway.registers import Register, RegisterMap, load_registers
from gateway.station import StationSnapshot

ALLOW, HOLD, DENY = "ALLOW", "HOLD", "DENY"
_RANK = {ALLOW: 0, HOLD: 1, DENY: 2}
_REG_KIND = {"coil": "coil", "register": "holding_register"}
ALWAYS_SETTABLE = ("SAFE_MODE", "ALARM")  # setting these to 1 only makes the plant safer

_regs: RegisterMap | None = None

# Context (who, when, how often). It explains a HOLD/DENY; it never makes a clean write held.
RATE_WINDOW_S, RATE_MAX = 10.0, 5   # more than RATE_MAX writes in the window is a burst
DAY_START_H, DAY_END_H = 6, 22      # expected operating hours, local time
_seen: set[str] = set()
_recent: dict[str, deque] = defaultdict(deque)


def reset_context() -> None:
    _seen.clear()
    _recent.clear()


def now() -> datetime:
    """Wall clock, unless DEMO_CLOCK ("HH:MM" or ISO timestamp) overrides it (demo: 3 am story)."""
    raw = os.environ.get("DEMO_CLOCK", "").strip()
    if raw:
        try:
            if ":" in raw and "T" not in raw and "-" not in raw:
                h, m = raw.split(":")[:2]
                return datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return datetime.fromisoformat(raw)
        except ValueError:
            pass  # bad setting: use the real clock rather than crash
    return datetime.now()


def context_reasons(req: WriteRequest, station: StationSnapshot) -> list[str]:
    reasons = []
    first = req.src_ip not in _seen
    _seen.add(req.src_ip)
    if first:
        reasons.append("source not seen before")
    t = time.monotonic()
    q = _recent[req.src_ip]
    q.append(t)
    while q and t - q[0] > RATE_WINDOW_S:
        q.popleft()
    if len(q) > RATE_MAX:
        reasons.append(f"{len(q)} writes in {RATE_WINDOW_S:g} s from this source")
    hour = now()
    if not DAY_START_H <= hour.hour < DAY_END_H:
        reasons.append(f"{hour:%H:%M} is outside operating hours")
    di = station.discrete_inputs
    if not di.get("PRESENCE") and not di.get("CABINET_OPEN"):
        reasons.append("nobody on site")
    return reasons


@dataclass(frozen=True)
class Decision:
    decision: str
    reasons: list[str] = field(default_factory=list)
    prediction: object | None = None  # physics Prediction, when one was made


# A check returns (verdict, reasons). `reg` is None when the address is not in the map.

def check_known(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    if reg is None:
        return DENY, [f"{req.kind} {req.address} is not in the register map"]
    if reg.twin_only:
        return DENY, [f"{reg.name} is written by the twin only"]
    return ALLOW, []


def check_envelope(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    if reg is None:
        return ALLOW, []
    if reg.max is not None and req.value > reg.max:
        return HOLD, [f"{reg.name} {req.value} is above max {reg.max}"]
    if reg.min is not None and req.value < reg.min:
        return HOLD, [f"{reg.name} {req.value} is below min {reg.min}"]
    return ALLOW, []


def check_safe_mode_clear(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    if reg is not None and reg.kind == "coil" and reg.name == "SAFE_MODE" and req.value == 0:
        return HOLD, ["clearing SAFE_MODE remotely needs operator approval"]
    return ALLOW, []


def check_physics(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    """Any predicted violation the write would cause is a HOLD. So is not knowing."""
    if reg is None or reg.twin_only:
        return ALLOW, []  # already denied, and the model has no such register
    try:
        state = physics_client.plant_state(station)
    except Exception as e:
        return HOLD, [f"station state unknown: {e}"]
    try:
        p = physics_client.check(
            state, physics_client.Command(kind=req.kind, address=req.address, value=req.value))
        new = [v for v in p.violations if not v.pre_existing]
        reasons = [f"{v.code} in {round(v.minutes_until)} min" for v in new]
        if not p.safe and not p.violations:
            reasons = ["physics check gave no verdict"]
    except TimeoutError:
        return HOLD, ["physics check timed out"]
    except Exception as e:
        return HOLD, [f"physics check failed: {e!r}"]
    return (HOLD if reasons else ALLOW), reasons, p


CHECKS = [check_known, check_envelope, check_safe_mode_clear, check_physics]


def decide(req: WriteRequest, station: StationSnapshot, *,
           regs: RegisterMap | None = None) -> Decision:
    global _regs
    if regs is None:
        regs = _regs = _regs or load_registers()
    reg = regs.lookup(_REG_KIND.get(req.kind, req.kind), req.address)

    if reg is not None and reg.kind == "coil" and reg.name in ALWAYS_SETTABLE and req.value == 1:
        return Decision(ALLOW)

    verdict, reasons, prediction = ALLOW, [], None
    for check in CHECKS:  # no early exit: collect every reason
        try:
            v, why, *extra = check(req, reg, station)
        except Exception as e:  # a broken check must not become permission
            v, why, extra = HOLD, [f"{getattr(check, '__name__', 'check')} failed: {e!r}"], []
        reasons += why
        if extra:
            prediction = extra[0]
        if _RANK[v] > _RANK[verdict]:
            verdict = v
    ctx = context_reasons(req, station)
    if verdict != ALLOW:
        reasons += ctx
    return Decision(verdict, reasons, prediction)
