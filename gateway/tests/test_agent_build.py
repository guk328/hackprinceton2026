"""Incident agent with a scripted model: steps, verdict, budget."""
import pytest
from langchain_core.messages import AIMessage

from fake_model import FakeModel, call
from gateway.agent.budget import Budget, BudgetExceeded, cost_usd
from gateway.agent.build import Session, build_agent
from gateway.agent.prompt import INCIDENT_PROMPT, brief
from gateway.agent.tools_read import make_read_tools
from gateway.events import EventStore
from gateway.station import StationSnapshot

EVENT = {"src_ip": "203.0.113.7", "function": 6, "kind": "register", "address": 0,
         "name": "DOSER_RATE_PCT", "old_value": 10, "new_value": 100, "decision": "HOLD",
         "reasons": ["CHLORINE_HIGH in 126 min"],
         "prediction": {"safe": False, "violations": [], "trajectory": [[0, 60.0, 1.0]] * 500},
         "station": {"presence": False, "cabinet_open": False, "level_pct": 60.0,
                     "chlorine_mgl": 1.0}}


class Poller:
    snapshot = StationSnapshot()
    last_good_logic = {}


@pytest.fixture
def store():
    s = EventStore("sqlite:///")
    yield s
    s.close()


def run(store, script, budget=None):
    event = store.save_event(EVENT, traffic_id=store.save_traffic("203.0.113.7", "c", 1, 6))
    model = FakeModel(script=script, prompts=[], tools_seen=[])
    agent = build_agent(make_read_tools(store, Poller()), model, budget=budget)
    session = Session(agent, event["id"],
                      lambda kind, text, data=None: store.save_agent_step(event["id"], kind, text, data))
    return event, model, session


def test_steps_are_stored_and_the_verdict_cites_ids(store):
    event, model, session = run(store, [
        call("get_event", event_id="evt_0001"),
        call("get_recent_traffic", src_ip="203.0.113.7"),
        AIMessage(content="MALICIOUS, high confidence. Evidence: evt_0001, traffic #1."),
    ])
    assert session.start(event) == "done"
    assert "evt_0001" in session.verdict and "traffic #1" in session.verdict
    steps = store.list_agent_steps()
    assert [s["kind"] for s in steps] == ["tool", "result", "tool", "result", "verdict"]
    assert steps[0]["text"].startswith("get_event(") and steps[0]["event_id"] == "evt_0001"
    assert '"decision": "HOLD"' in steps[1]["text"] or "HOLD" in steps[1]["text"]
    # the model saw our prompt, the brief without the trajectory, and only read tools of ours
    first = model.prompts[0]
    assert INCIDENT_PROMPT[:60] in str(first[0].content)
    assert "evt_0001" in str(first[-1].content) and "trajectory" not in str(first[-1].content)
    ours = set(model.tools_seen) - {"ls", "read_file", "write_file", "edit_file", "delete",
                                    "glob", "grep", "task"}
    assert ours == {"get_event", "get_recent_traffic", "read_station", "run_physics",
                    "diff_logic_baseline"}


def test_brief_is_small():
    assert len(brief({**EVENT, "id": "evt_0001"})) < 600


def test_budget_counts_and_stops_at_the_ceiling(store):
    budget = Budget(store, ceiling_usd=cost_usd(1000, 100) * 1.5)
    event, model, session = run(store, [
        call("get_event", event_id="evt_0001"),
        call("read_station"),
        AIMessage(content="never reached"),
    ], budget)
    budget.start_run(event["id"])
    with pytest.raises(BudgetExceeded):
        session.start(event)
    assert budget.calls == 2 and len(model.prompts) == 2      # third call was refused
    assert store.total_spend() == pytest.approx(2 * cost_usd(1000, 100))


def test_cost_formula():
    assert cost_usd(50_000, 0) == pytest.approx(0.005)
    assert cost_usd(0, 10_000) == pytest.approx(0.005)
    assert cost_usd(200_000, 0) == pytest.approx(0.10)        # 5x above 100K


def test_model_call_limit_ends_the_run(store):
    event, model, session = run(store, [call("read_station", _id=f"c{i}") for i in range(30)])
    session.agent = build_agent(make_read_tools(store, Poller()), model, max_model_calls=3)
    assert session.start(event) == "done"
    assert len(model.prompts) == 3
