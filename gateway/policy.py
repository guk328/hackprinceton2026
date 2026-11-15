"""decide(): the allow/hold/deny rule for one write. Deterministic, no LLM, fails closed.

Every check runs and every reason is kept; the strictest verdict wins (DENY > HOLD > ALLOW).

Two kinds of check. SAFETY_CHECKS (register known, envelope, physics) can never be
overridden by anyone. APPROVAL_CHECKS (clearing SAFE_MODE, a large step, an unknown source)
hold a write that is safe but unusual until the operator says ALLOW. decide(release=True) is the re-check before a held command is
forwarded: it runs the safety checks only, on fresh station state.
"""
import ipaddress
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from gateway import physics_client
from gateway.frames import WriteRequest
from gateway.registers import Register, RegisterMap, load_registers
from gateway.station import StationSnapshot

ALLOW, HOLD, DENY = "ALLOW", "HOLD", "DENY"
_RANK = {ALLOW: 0, HOLD: 1, DENY: 2}
_REG_KIND = {"coil": "coil", "register": "holding_register"}
# Setting these to 1 is never refused: they are the emergency stop. It is not free of harm:
# SAFE_MODE turns the doser off (the model shows low chlorine in about 14 plant-hours), so
# switching it on is allowed but reported (SAFE_MODE_SET), never silent.
ALWAYS_SETTABLE = ("SAFE_MODE", "ALARM")

SAFE_MODE_SET = "SAFE_MODE_SET"   # first word of the reason on that ALLOW event; the runner opens an incident
SHUT_OFF = ("ALARM", "DOSER_CMD", "DOSER_RATE_PCT")   # writing 0 silences the alarm or stops chlorination

_regs: RegisterMap | None = None

# Context (who, when, how often). The reasons explain a HOLD/DENY; check_source is the rule
# that holds on them.
RATE_WINDOW_S, RATE_MAX = 10.0, 5   # more than RATE_MAX writes in the window is a burst
DAY_START_H, DAY_END_H = 6, 22      # expected operating hours, local time
_seen: set[str] = set()
_recent: dict[str, deque] = defaultdict(deque)
# Large-step rule: what each setting was over the last STEP_WINDOW_S, so a walk in small
# steps is measured from where it started. name -> (time last seen, value the station held).
STEP_WINDOW_S = 30 * 60
_levels: dict[str, deque] = defaultdict(lambda: deque(maxlen=64))
monotonic = time.monotonic   # tests move this clock


def reset_context() -> None:
    _seen.clear()
    _recent.clear()
    _levels.clear()


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


# Operator addresses: OPERATOR_IPS in .env, comma separated, single addresses or CIDR ranges.
# Not set at all: only this machine. Set but empty: nobody. An entry that does not parse is
# skipped, which trusts fewer sources, never more.
DEFAULT_OPERATOR_IPS = "127.0.0.1"


def is_operator(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in os.environ.get("OPERATOR_IPS", DEFAULT_OPERATOR_IPS).split(","):
        try:
            if entry.strip() and addr in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def is_night() -> bool:
    return not DAY_START_H <= now().hour < DAY_END_H


def on_site(station: StationSnapshot) -> bool:
    di = station.discrete_inputs
    return bool(di.get("PRESENCE") or di.get("CABINET_OPEN"))


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
    if is_night():
        reasons.append(f"{now():%H:%M} is outside operating hours")
    if not on_site(station):
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


# Limits a trajectory can overshoot: code -> (trajectory column, +1 above / -1 below the limit).
_EXTREME = {"CHLORINE_HIGH": (2, 1), "CHLORINE_LOW": (2, -1),
            "TANK_OVERFLOW": (1, 1), "PRESSURE_LOSS": (1, -1)}
_EPS = 1e-6


def worsened(v, p) -> str | None:
    """A violation the plant is heading for anyway (pre_existing) is not the command's
    fault, unless the command brings it sooner or pushes further past the limit. Returns
    why it counts against the command, or None. Not being able to compare counts."""
    base = getattr(v, "baseline_minutes_until", None)
    if base is None:
        return "no baseline to compare with"
    if v.minutes_until < base - _EPS:
        return f"{round(base)} min without this command"
    column, cand, without = _EXTREME.get(v.code), p.trajectory, getattr(p, "baseline_trajectory", None)
    if column and cand and without:
        i, sign = column
        if max(sign * row[i] for row in cand) > max(sign * row[i] for row in without) + _EPS:
            return "further past the limit than without this command"
    return None


def check_physics(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    """Any predicted violation the write would cause, bring sooner or make worse is a HOLD.
    So is not knowing."""
    if reg is None or reg.twin_only:
        return ALLOW, []  # already denied, and the model has no such register
    try:
        state = physics_client.plant_state(station)
    except Exception as e:
        return HOLD, [f"station state unknown: {e}"]
    try:
        p = physics_client.check(
            state, physics_client.Command(kind=req.kind, address=req.address, value=req.value))
        reasons, judged = [], []
        for v in p.violations:
            why = worsened(v, p) if v.pre_existing else None
            if why:        # stored as the command's own, so the feed and the report show it
                v = replace(v, pre_existing=False)
            if not v.pre_existing:
                reasons.append(f"{v.code} in {round(v.minutes_until)} min" + (f" ({why})" if why else ""))
            judged.append(v)
        if judged != list(p.violations):
            p = replace(p, violations=judged)
        if not p.safe and not p.violations:
            reasons = ["physics check gave no verdict"]
    except TimeoutError:
        return HOLD, ["physics check timed out"]
    except Exception as e:
        return HOLD, [f"physics check failed: {e!r}"]
    return (HOLD if reasons else ALLOW), reasons, p


LARGE_STEP_FRACTION = 0.25   # of the register's envelope (max - min)


def check_large_change(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    """A big jump in a control setting waits for the operator even when the model says it
    is safe (scenario 5). The jump is measured from every value the setting had in the last
    STEP_WINDOW_S, not only the current one, so 10 -> 35 -> 60 is a step of 50. The
    operator's ALLOW overrides this, and only this."""
    if reg is None or reg.kind != "holding_register" or reg.min is None or reg.max is None:
        return ALLOW, []
    old = station.holding.get(reg.name)
    if old is None:
        return ALLOW, []   # unknown state: the physics check holds
    limit = LARGE_STEP_FRACTION * (reg.max - reg.min)
    t, seen = monotonic(), _levels[reg.name]
    while seen and t - seen[0][0] > STEP_WINDOW_S:
        seen.popleft()
    if seen and seen[-1][1] == old:
        seen.pop()
    seen.append((t, old))          # only values read from the station, never a requested one
    start = max((v for _, v in seen), key=lambda v: abs(req.value - v))
    if abs(req.value - start) > limit:
        span = "" if start == old else f" within {STEP_WINDOW_S // 60} min"
        return HOLD, [f"{reg.name} {start} -> {req.value}{span} is a large step (over {limit:g}): "
                      "needs operator approval"]
    return ALLOW, []


def check_source(req: WriteRequest, reg: Register | None, station: StationSnapshot):
    """A setting change from an address that is not on OPERATOR_IPS waits for the operator
    when it is night or nobody is on site, however harmless it looks. The context reasons
    added after it say which of the two applied. Switching off the alarm or the chlorine
    dose from such an address always waits: the harm is slower than the physics horizon."""
    if reg is None or is_operator(req.src_ip):
        return ALLOW, []
    if req.value == 0 and reg.name in SHUT_OFF:      # at any hour, whoever is on site
        return HOLD, [f"{reg.name} -> 0 from {req.src_ip}, not an operator address: turning off the "
                      "alarm or the chlorine dose needs operator approval"]
    if is_night() or not on_site(station):
        return HOLD, [f"source {req.src_ip} is not an operator address: needs operator approval"]
    return ALLOW, []


SAFETY_CHECKS = [check_known, check_envelope, check_physics]
APPROVAL_CHECKS = [check_safe_mode_clear, check_large_change, check_source]
CHECKS = SAFETY_CHECKS + APPROVAL_CHECKS


def decide(req: WriteRequest, station: StationSnapshot, *,
           regs: RegisterMap | None = None, release: bool = False) -> Decision:
    global _regs
    if regs is None:
        regs = _regs = _regs or load_registers()
    reg = regs.lookup(_REG_KIND.get(req.kind, req.kind), req.address)

    if reg is not None and reg.kind == "coil" and reg.name in ALWAYS_SETTABLE and req.value == 1:
        if reg.name == "SAFE_MODE" and not release and station.coils.get("SAFE_MODE") != 1:
            # Never refused, but never silent: safe mode stops the doser, so the operator hears of it.
            return Decision(ALLOW, [f"{SAFE_MODE_SET}: safe mode switched on from {req.src_ip}; the doser "
                                    "is off and settings are locked until it is cleared"])
        return Decision(ALLOW)

    verdict, reasons, prediction = ALLOW, [], None
    for check in (SAFETY_CHECKS if release else CHECKS):  # no early exit: collect every reason
        try:
            v, why, *extra = check(req, reg, station)
        except Exception as e:  # a broken check must not become permission
            v, why, extra = HOLD, [f"{getattr(check, '__name__', 'check')} failed: {e!r}"], []
        reasons += why
        if extra:
            prediction = extra[0]
        if _RANK[v] > _RANK[verdict]:
            verdict = v
    if release:           # the operator has already weighed who, when and how big
        if verdict == ALLOW and reg is not None:
            _levels.pop(reg.name, None)   # the approved value is the new starting point
        return Decision(verdict, reasons, prediction)
    ctx = context_reasons(req, station)
    if verdict != ALLOW:
        reasons += ctx
    return Decision(verdict, reasons, prediction)
