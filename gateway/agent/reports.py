"""Incident reports: gateway/reports/<event_id>.md, served at GET /reports/{id}.

    make_save_report(store, reports_dir)  the reporter subagent's one write tool
    template_report(store, event_id)      the same report from stored facts, no model

A dedicated tool instead of general file tools: it can only write one file per incident,
named by a checked event id, inside the reports folder.
"""
import re
from pathlib import Path

EVENT_ID = re.compile(r"^evt_\d{4,12}$")
MAX_REPORT_CHARS = 20_000


def write_report(store, reports_dir: Path, event_id: str, markdown: str) -> dict:
    if not isinstance(event_id, str) or not EVENT_ID.match(event_id):
        return {"saved": False, "error": f"not an event id: {event_id!r}"}
    if store.get_event(event_id) is None:
        return {"saved": False, "error": f"no event {event_id}"}
    markdown = (markdown or "").strip()
    if len(markdown) < 80 or event_id not in markdown:
        return {"saved": False, "error": "the report must describe the incident and cite its event id"}
    if len(markdown) > MAX_REPORT_CHARS:
        return {"saved": False, "error": f"report is over {MAX_REPORT_CHARS} characters"}
    reports_dir.mkdir(parents=True, exist_ok=True)
    tmp = reports_dir / f".{event_id}.tmp"
    tmp.write_text(markdown + "\n", encoding="utf-8")
    tmp.replace(reports_dir / f"{event_id}.md")
    store.save_agent_step(event_id, "report", f"Incident report written for {event_id}.")
    return {"saved": True, "report": f"/reports/{event_id}", "characters": len(markdown)}


def make_save_report(store, reports_dir: Path):
    def save_report(event_id: str, markdown: str) -> dict:
        """Save the incident report for this event as Markdown (replaces an earlier one).
        It must cite the event id and the traffic row ids behind its timeline."""
        return write_report(store, reports_dir, event_id, markdown)
    return save_report


def template_report(store, event_id: str) -> str:
    """A complete report from what the gateway stored. Used when the agent wrote none."""
    e = store.get_event(event_id) or {}
    p = e.get("prediction") or {}
    st = e.get("station") or {}
    rows = store.query_traffic(src_ip=e.get("src_ip"), minutes=24 * 60, limit=30)
    steps = store.list_agent_steps(400, event_id)
    acts = [s for s in steps if s["kind"] in ("action", "released", "refused", "text", "reply")]
    reply = next((s["text"] for s in steps if s["kind"] == "reply"), "No reply recorded.")
    what = e.get("name") or f"function {e.get('function')}"
    out = [
        f"# Incident report {event_id}", "",
        "_Written by the gateway from its own records (the agent did not write this one)._", "",
        "## What happened",
        f"At {e.get('ts')} the gateway received a write from {e.get('src_ip')}: {what} "
        f"{e.get('old_value')} -> {e.get('new_value')} (event {event_id}, traffic #{e.get('traffic_id')}). "
        f"Decision: **{e.get('decision')}**. The command did not reach the station.", "",
        "Reasons recorded: " + ("; ".join(e.get("reasons") or []) or "none") + ".", "",
        "## Timeline (traffic from this source, newest 30 frames)",
    ]
    out += [f"- {r['ts']} traffic #{r['id']}: {r['direction']}, function {r['fc']}, "
            f"address {r['address']}, values {r['values']}" for r in rows] or ["- no frames stored"]
    out += ["", "## What the physics model predicted"]
    viol = [v for v in p.get("violations") or [] if not v.get("pre_existing")]
    out += [f"- {v.get('message')} (limit {v.get('limit')})" for v in viol] or \
           ["- No new violation predicted." if p else "- No prediction was made for this command."]
    out += ["", f"Station at the time: level {st.get('level_pct')} %, chlorine {st.get('chlorine_mgl')} mg/L, "
            f"someone on site: {'yes' if st.get('presence') or st.get('cabinet_open') else 'no'}.", "",
            "## Actions taken"]
    out += [f"- {s['ts']} {s['text']}" for s in acts] or ["- The command was held. No further action."]
    out += ["", "## Operator decision", reply, "",
            "## Recommended follow-ups",
            "- Find out who or what sent the command and whether that source should reach the station at all.",
            "- Check the station's control settings against the last known good copy.",
            "- Keep this report and the gateway database as evidence.", "",
            "## For the state primacy agency and CISA",
            f"Unauthorized or unsafe control command against a water pump station, stopped by the gateway "
            f"before reaching the controller. Source {e.get('src_ip')}, time {e.get('ts')}, target {what}. "
            "No effect on water quality or pressure. Report to CISA at report@cisa.gov or 1-844-729-2472.", ""]
    return "\n".join(out)
