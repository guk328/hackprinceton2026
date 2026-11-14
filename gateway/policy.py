"""decide(): the allow/hold/deny rule for one write. Deterministic, no LLM, fails closed.

Every check runs and every reason is kept; the strictest verdict wins (DENY > HOLD > ALLOW).
"""
from dataclasses import dataclass, field

from gateway import physics_client
from gateway.frames import WriteRequest
from gateway.registers import Register, RegisterMap, load_registers
from gateway.station import StationSnapshot

ALLOW, HOLD, DENY = "ALLOW", "HOLD", "DENY"
_RANK = {ALLOW: 0, HOLD: 1, DENY: 2}
_REG_KIND = {"coil": "coil", "register": "holding_register"}
ALWAYS_SETTABLE = ("SAFE_MODE", "ALARM")  # setting these to 1 only makes the plant safer

_regs: RegisterMap | None = None


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
    return Decision(verdict, reasons, prediction)
