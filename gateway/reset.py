"""Demo reset: back to a clean, repeatable start without restarting anything.

Clears events, traffic and agent steps (ids start again at evt_0001), the blocklist, held
commands, open operator questions, the policy's memory of sources, agent threads and
reports, and puts the station's coils and control settings back to the register-map
defaults. The spend ledger is kept: money already spent stays counted against the ceiling.

Admin only (POST /reset), or at start-up with BACKFLOW_RESET_ON_START=1 (tasks.py demo).
The twin owns tank level and chlorine; restart it with --reset for those.
"""
import logging
from pathlib import Path

from gateway import policy

log = logging.getLogger(__name__)


def reset(store, *, blocklist=None, held=None, channel=None, runner=None, poller=None,
          writer=None, reports_dir: Path | None = None) -> dict:
    out = {"station": "not touched"}
    if runner is not None:
        runner.reset()
    for part in (blocklist, held, channel):
        if part is not None:
            part.clear()
    policy.reset_context()
    store.clear()
    if reports_dir is not None and reports_dir.is_dir():
        for f in reports_dir.glob("evt_*.md"):
            f.unlink()
    if writer is not None:
        try:
            logic = writer.restore_defaults()
            if poller is not None:
                poller.last_good_logic = dict(logic)
            out["station"] = "defaults restored"
        except Exception as e:      # station off: the rest of the reset still stands
            log.warning("could not restore station defaults: %r", e)
            out["station"] = f"not restored: {e}"
    log.info("demo reset done (%s)", out["station"])
    return {"reset": True, **out}
