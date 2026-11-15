"""Build the incident agent and drive one incident thread.

    agent = build_agent(tools, model=..., budget=...)
    s = Session(agent, "evt_0042", on_step=print_step)
    s.start(event)        -> "done" | "waiting" (paused at a tool that needs approval)
    s.resume(approve)     -> answer the pause
    s.tell(text)          -> a further message on the same thread

The agent is never in the allow/hold path: it only sees events the gateway already decided.
deepagents 0.7.38 API checked in site-packages: create_deep_agent(model, tools, system_prompt=,
middleware=, subagents=, interrupt_on=, checkpointer=, backend=); an interrupt arrives in
stream(..., stream_mode="updates") as {"__interrupt__": (Interrupt(value={"action_requests":
[...], "review_configs": [...]}),)} and is answered with
Command(resume={"decisions": [{"type": "approve"} | {"type": "reject", "message": ...}]}).
"""
import json
import os

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from gateway.agent.prompt import INCIDENT_PROMPT, brief

DEFAULT_MODEL = "anthropic:claude-haiku-5-5"
MAX_OUTPUT_TOKENS = 1200     # per model call
MAX_MODEL_CALLS = 14         # per run (start, resume and tell are each a run)
MODEL_TIMEOUT_S = 40
STEP_TEXT_MAX = 600
# The run pauses at this tool until the operator answers; they can only say yes or no.
INTERRUPT_ON = {"release_held_command": {"allowed_decisions": ["approve", "reject"]}}


def make_model(name: str | None = None):
    """The chat model named by AGENT_MODEL ("provider:model")."""
    from langchain.chat_models import init_chat_model
    return init_chat_model(name or os.getenv("AGENT_MODEL") or DEFAULT_MODEL,
                           max_tokens=MAX_OUTPUT_TOKENS, timeout=MODEL_TIMEOUT_S, max_retries=1)


def build_agent(tools, model=None, *, budget=None, interrupt_on=None, subagents=None,
                checkpointer=None, backend=None, system_prompt: str = INCIDENT_PROMPT,
                max_model_calls: int = MAX_MODEL_CALLS):
    model = model or make_model()
    if budget is not None:   # on the model itself, so subagent calls are counted too
        model.callbacks = [*(model.callbacks or []), budget]
    return create_deep_agent(
        model=model, tools=list(tools), system_prompt=system_prompt,
        middleware=[ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end")],
        interrupt_on=interrupt_on or None, subagents=subagents or None, backend=backend,
        checkpointer=checkpointer or InMemorySaver())


def _text(content) -> str:
    """Message content as plain text (Anthropic returns a list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content or []]
    return "\n".join(p for p in parts if p).strip()


def _clip(s: str, n: int = STEP_TEXT_MAX) -> str:
    return s if len(s) <= n else s[:n] + "..."


class Session:
    """One incident = one checkpointed thread (thread_id = event id)."""

    def __init__(self, agent, event_id: str, on_step=None, recursion_limit: int = 80):
        self.agent, self.event_id = agent, event_id
        self.on_step = on_step or (lambda kind, text, data=None: None)
        self.cfg = {"configurable": {"thread_id": event_id}, "recursion_limit": recursion_limit}
        self.pending: list[dict] = []     # action requests waiting for approval
        self.verdict = ""                 # last plain answer from the agent
        self._seen: set[str] = set()

    def start(self, event: dict) -> str:
        return self._drive({"messages": [{"role": "user", "content": brief(event)}]})

    def tell(self, text: str) -> str:
        return self._drive({"messages": [{"role": "user", "content": text}]})

    def resume(self, approve: bool, message: str = "") -> str:
        d = {"type": "approve"} if approve else {"type": "reject", "message": message}
        return self._drive(Command(resume={"decisions": [d] * max(1, len(self.pending))}))

    def _drive(self, inp) -> str:
        self.pending = []
        for chunk in self.agent.stream(inp, self.cfg, stream_mode="updates"):
            for node, upd in chunk.items():
                if node == "__interrupt__":
                    self.pending = list(upd[0].value.get("action_requests", []))
                    for a in self.pending:
                        self.on_step("waiting", f"{a['name']} needs the operator's approval",
                                     {"name": a["name"], "args": a.get("args")})
                    continue
                if not isinstance(upd, dict):
                    continue
                msgs = upd.get("messages") or []
                for m in getattr(msgs, "value", msgs):
                    self._emit(m)
        return "waiting" if self.pending else "done"

    def _emit(self, m):
        mid = getattr(m, "id", None)
        if mid:
            if mid in self._seen:     # a resumed turn replays the message it paused on
                return
            self._seen.add(mid)
        kind = type(m).__name__
        if kind == "AIMessage":
            text, calls = _text(m.content), getattr(m, "tool_calls", None) or []
            if text and calls:
                self.on_step("note", _clip(text))
            for c in calls:
                self.on_step("tool", f"{c['name']}({json.dumps(c.get('args') or {})})",
                             {"name": c["name"], "args": c.get("args")})
            if text and not calls:
                self.verdict = text
                self.on_step("verdict", text)
        elif kind == "ToolMessage":
            self.on_step("result", _clip(_text(m.content)), {"name": getattr(m, "name", None)})
