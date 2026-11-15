"""Action tools for the incident agent. Deliberately few, and none takes a register
address or value: the agent cannot compose a Modbus write.

    block_source(ip, minutes)        the proxy drops this source
    revert_to_last_known_good()      holding 0-4 back to the gateway's stored copy
    enter_safe_mode()                SAFE_MODE and ALARM on
    notify_operator(event_id, text)  one text per incident
    release_held_command(event_id)   forward a held frame; see `release` below

Each returns a small dict and never raises.
"""
import logging

from gateway import policy
from gateway.blocklist import Blocklist
from gateway.notify import OperatorChannel
from gateway.station import LOGIC_ADDRS
from gateway.station_write import StationWriter

log = logging.getLogger(__name__)


def make_action_tools(store, poller, blocklist: Blocklist, channel: OperatorChannel,
                      writer: StationWriter, held=None, refresh=None, lock=None) -> list:
    """`held` is the proxy's HeldStore and `refresh` a callable returning a fresh
    StationSnapshot; without `held` there is no release tool. `lock(fn)` runs fn under the
    proxy's write lock (Proxy.run_locked)."""

    def block_source(ip: str, minutes: int = 60) -> dict:
        """Make the gateway drop every connection from this source IP for `minutes`
        (1 to 1440). Use for a source you judge malicious. Reads and writes are both dropped."""
        try:
            until = blocklist.block(ip, minutes)
        except (ValueError, TypeError) as e:
            return {"blocked": False, "error": f"not blocked: {e}"}
        return {"blocked": True, "ip": ip, "until_epoch": round(until)}

    def revert_to_last_known_good() -> dict:
        """Rewrite the station's control settings (holding 0-4: doser rate, level setpoints,
        chlorine target, logic version) from the gateway's last known good copy. Use when
        diff_logic_baseline shows a change the gateway did not allow."""
        good = dict(getattr(poller, "last_good_logic", {}) or {})
        try:
            values = writer.restore_logic(good)
        except Exception as e:
            return {"reverted": False, "error": str(e)}
        return {"reverted": True, "holding_0_4": values}

    def enter_safe_mode() -> dict:
        """Put the station in SAFE_MODE (doser off, pump on local level control, remote
        setting changes ignored) and sound the ALARM. Strong action: use only when the
        station itself may be compromised, not for a command that was simply held."""
        try:
            writer.safe_mode()
        except Exception as e:
            return {"safe_mode": False, "error": str(e)}
        return {"safe_mode": True, "alarm": True}

    def notify_operator(event_id: str, text: str) -> dict:
        """Send the on-call operator one plain-English text about this event (under 320
        characters, once per event). Say what happened, what it would have done to the water,
        what you did, and end with "Reply BLOCK or ALLOW". The gateway adds the reply code."""
        if store.get_event(event_id) is None:
            return {"sent": False, "error": f"no event {event_id}"}
        return channel.send(event_id, text)

    tools = [block_source, revert_to_last_known_good, enter_safe_mode, notify_operator]
    if held is None:
        return tools

    def release_held_command(event_id: str) -> dict:
        """Forward a held command to the station. Needs the operator's ALLOW for this event
        (the run pauses here until they answer). The gateway re-checks the command against
        the envelope and the physics model first and refuses if it is unsafe now, whatever
        the operator said. Only ask for this when you judge the command legitimate."""
        return release(event_id, held=held, channel=channel, writer=writer, refresh=refresh,
                       store=store, poller=poller, lock=lock)

    return tools + [release_held_command]


def release(event_id: str, *, held, channel, writer, refresh, store=None, poller=None,
            lock=None) -> dict:
    """The only path that forwards a held frame. Plain code, in this order, all required:
    1. the frame is still held, 2. a verified operator ALLOW exists for this event, given
    less than APPROVAL_TTL_S ago (used up here), 3. every write in it passes
    policy.decide(release=True) against fresh station state: envelope, SAFE_MODE rule and
    physics, without the context and approval rules the operator has just overridden.
    Steps 3 and the forward run under the proxy's write lock (`lock`), so no other write can
    land between the re-check and the forward. Any failure or error leaves the frame
    unforwarded."""
    def recheck_and_forward() -> dict:
        if held.get(cmd.id) is None:
            return {"released": False, "error": f"{event_id} is no longer held"}
        snap = refresh()
        from gateway.proxy import with_write
        for req in cmd.writes:
            d = policy.decide(req, snap, release=True)
            if d.decision != policy.ALLOW:
                held.pop(cmd.id)      # refused for safety: it can never be released later
                if store is not None:
                    store.save_agent_step(event_id, "refused",
                                          "Release refused by the safety checks: " + "; ".join(d.reasons))
                return {"released": False, "error": "refused by the safety checks",
                        "reasons": d.reasons}
            snap = with_write(snap, req)
        held.pop(cmd.id)
        reply = writer.forward(cmd.frame.raw)
        if len(reply) > 7 and reply[7] & 0x80:
            return {"released": False, "error": f"station answered exception {reply[8]:#04x}"}
        if poller is not None:
            poller.accept_logic({w.address: w.value for w in cmd.writes if w.kind == "register"})
        if store is not None:
            store.save_agent_step(event_id, "released", "Held command forwarded to the station")
        return {"released": True, "event_id": event_id,
                "writes": [{"name": w.name, "value": w.value} for w in cmd.writes]}

    try:
        cmd = held.by_event(event_id)
        if cmd is None:
            return {"released": False, "error": f"nothing is held for {event_id}"}
        if not channel.take_approval(event_id):
            return {"released": False,
                    "error": "no verified operator ALLOW for this event (none given, already "
                             "used, or older than 5 minutes)"}
        return lock(recheck_and_forward) if lock is not None else recheck_and_forward()
    except Exception as e:
        log.exception("release failed")
        return {"released": False, "error": f"release failed: {e!r}"}
