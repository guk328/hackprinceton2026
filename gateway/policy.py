"""decide(): the allow/hold/deny rule for one write. Deterministic, no LLM, fails closed.

Every check runs and every reason is kept; the strictest verdict wins (DENY > HOLD > ALLOW).
"""
from dataclasses import dataclass, field

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


CHECKS = [check_known, check_envelope, check_safe_mode_clear]


def decide(req: WriteRequest, station: StationSnapshot, *,
           regs: RegisterMap | None = None) -> Decision:
    global _regs
    if regs is None:
        regs = _regs = _regs or load_registers()
    reg = regs.lookup(_REG_KIND.get(req.kind, req.kind), req.address)

    if reg is not None and reg.kind == "coil" and reg.name in ALWAYS_SETTABLE and req.value == 1:
        return Decision(ALLOW)

    verdict, reasons = ALLOW, []
    for check in CHECKS:  # no early exit: collect every reason
        try:
            v, why = check(req, reg, station)
        except Exception as e:  # a broken check must not become permission
            v, why = HOLD, [f"{getattr(check, '__name__', 'check')} failed: {e!r}"]
        reasons += why
        if _RANK[v] > _RANK[verdict]:
            verdict = v
    return Decision(verdict, reasons)
