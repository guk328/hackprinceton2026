"""Subagents and the incident report, model mocked."""
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from fake_model import FakeModel, call
from gateway.agent.budget import Budget
from gateway.agent.build import INTERRUPT_ON, Session, build_agent
from gateway.agent.reports import make_save_report, template_report, write_report
from gateway.agent.subagents import make_subagents
from gateway.agent.tools_act import make_action_tools
from gateway.agent.tools_read import make_read_tools
from gateway.api import create_app
from gateway.blocklist import Blocklist
from gateway.events import EventStore
from gateway.held import HeldStore
from gateway.notify import OperatorChannel
from test_agent_build import EVENT, Poller
from test_block import PUMP, RATE, on_station  # noqa: F401
from test_proxy import HOST, station  # noqa: F401 (fixture)
from test_runner import Gw

READ = {"get_event", "get_recent_traffic", "read_station", "run_physics", "diff_logic_baseline"}
ACT = {"block_source", "revert_to_last_known_good", "enter_safe_mode", "notify_operator",
       "release_held_command"}
REPORT = """# Incident report evt_0001
## What happened
A write of DOSER_RATE_PCT 10 -> 100 from 203.0.113.7 was held (evt_0001, traffic #1).
## Timeline
- 03:12:09 write held [evt_0001, traffic #1]
"""


@pytest.fixture
def store():
    s = EventStore("sqlite:///:memory:")
    s.save_event(EVENT, traffic_id=s.save_traffic("203.0.113.7", "client->station", 1, 6, 0, 1, [100]))
    yield s
    s.close()


def test_main_agent_delegates_and_the_report_is_served(store, tmp_path):
    read = make_read_tools(store, Poller())
    act = make_action_tools(store, Poller(), Blocklist(), OperatorChannel(store), None,
                            held=HeldStore(), refresh=lambda: None)
    budget = Budget(store, ceiling_usd=1)
    model = FakeModel(prompts=[], tools_seen=[], binds=[], script=[
        call("task", _id="t1", subagent_type="forensics", description="Timeline for evt_0001"),
        call("get_event", _id="f1", event_id="evt_0001"),                       # inside forensics
        AIMessage(content="- 03:12:09 write held [evt_0001, traffic #1]"),      # forensics answer
        call("task", _id="t2", subagent_type="reporter", description="Report for evt_0001: ..."),
        call("save_report", _id="r1", event_id="evt_0001", markdown=REPORT),    # inside reporter
        AIMessage(content="Saved."),
        AIMessage(content="Final verdict: MALICIOUS. evt_0001, traffic #1. Report written."),
    ])
    agent = build_agent(read + act, model, budget=budget, interrupt_on=INTERRUPT_ON,
                        subagents=make_subagents(read, make_save_report(store, tmp_path)))
    s = Session(agent, "evt_0001", lambda k, t, d=None: store.save_agent_step("evt_0001", k, t, d))
    assert s.start(store.get_event("evt_0001")) == "done"
    assert (tmp_path / "evt_0001.md").read_text().startswith("# Incident report evt_0001")
    assert "report" in [x["kind"] for x in store.list_agent_steps()]
    assert budget.calls == 7                                   # subagent calls are counted too
    # no subagent is ever offered an action tool; only the reporter can save a report
    custom = [set(b) & (READ | ACT | {"save_report"}) for b in model.binds]
    assert READ | ACT in custom and READ in custom and READ | {"save_report"} in custom
    assert all(c == READ | ACT or not (c & ACT) for c in custom)
    client = TestClient(create_app(store, reports_dir=tmp_path, dashboard_dir=tmp_path / "none"))
    assert client.get("/reports/evt_0001").text.startswith("# Incident report evt_0001")


def test_save_report_checks_its_input(store, tmp_path):
    save = make_save_report(store, tmp_path)
    assert save("../../etc/passwd", REPORT)["saved"] is False
    assert save("evt_9999", REPORT.replace("0001", "9999"))["saved"] is False
    assert save("evt_0001", "too short")["saved"] is False
    assert save("evt_0001", "x" * 30000 + "evt_0001")["saved"] is False
    assert list(tmp_path.iterdir()) == []
    assert save("evt_0001", REPORT)["saved"] is True
    assert [p.name for p in tmp_path.iterdir()] == ["evt_0001.md"]


def test_template_report_has_every_section(store, tmp_path):
    store.save_agent_step("evt_0001", "reply", "Operator replied BLOCK")
    text = template_report(store, "evt_0001")
    for h in ("What happened", "Timeline", "physics model predicted", "Actions taken",
              "Operator decision", "Recommended follow-ups", "CISA"):
        assert h in text
    assert "traffic #1" in text and "Operator replied BLOCK" in text
    assert write_report(store, tmp_path, "evt_0001", text)["saved"] is True


def test_runner_writes_the_report_itself_when_there_is_no_model(station, tmp_path):
    g = Gw(station, script=None)
    g.runner.reports_dir = tmp_path
    try:
        eid = g.attack()
        assert g.wait(lambda: g.channel.inbox())
        assert g.channel.reply(eid, "BLOCK", g.code(eid))[0]
        assert g.wait(lambda: (tmp_path / f"{eid}.md").is_file())
        text = (tmp_path / f"{eid}.md").read_text()
        assert eid in text and "blocked" in text and "Operator replied BLOCK" in text
    finally:
        g.close()
