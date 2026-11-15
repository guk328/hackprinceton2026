"""Small shared CLI helpers for the two owned-rig demo scenarios."""
import argparse
import json


def emit(event, **values):
    print(json.dumps({"event": event, **values}, allow_nan=False), flush=True)


def arguments(description, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default="127.0.0.1", help="our gateway host")
    parser.add_argument("--port", type=int, default=1502, help="our gateway port (not the station)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.port in (502, 5020):
        parser.error("target the gateway, normally port 1502; never the station port")
    return args


def read(link, kind, address, name):
    emit("request", operation="read", kind=kind, address=address, name=name)
    value = link.read(kind, address)
    emit("response", operation="read", name=name, value=value)
    return value


def write(link, kind, address, value, name, expected="ALLOW"):
    emit("request", operation="write", kind=kind, address=address, name=name,
         value=value, unit_id=link.regs.unit_id)
    # Link.write raises on all exceptions alike. Use its persistent client here
    # so the scenario can distinguish the documented HOLD code from other errors.
    if kind == "coil":
        response = link.client.write_coil(address, bool(value), device_id=link.regs.unit_id)
    else:
        response = link.client.write_register(address, value, device_id=link.regs.unit_id)
    error = response.isError()
    code = getattr(response, "exception_code", None) if error else None
    decision = "HOLD" if error and code == 6 else "ERROR" if error else "ALLOW"
    emit("response", operation="write", name=name, decision=decision,
         exception_code=code, detail=str(response))
    if decision != expected:
        raise RuntimeError(f"{name}: expected {expected}, received {decision} (exception={code})")
    return response
