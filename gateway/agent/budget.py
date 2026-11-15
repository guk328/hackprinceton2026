"""Model spend: counted per call, stored in the event store, capped.

A Budget is a LangChain callback attached to the chat model itself, so every call is
seen, subagents included. Before a call it raises BudgetExceeded if the ceiling is
reached or the prompt looks too large; the runner then falls back to templated text.
After a call it records the tokens the provider reported.

Prices are for AGENT_MODEL=anthropic:claude-haiku-5-5. Change them with the model.
The estimate is on the high side: cache reads are charged as plain input.
"""
import os
import threading

from langchain_core.callbacks import BaseCallbackHandler

IN_USD_PER_M, OUT_USD_PER_M = 0.10, 0.50
LONG_PROMPT_TOKENS, LONG_PROMPT_MULT = 100_000, 5   # prompts above this cost 5x
MAX_PROMPT_TOKENS = 80_000                           # we refuse to send more (estimate)
TOOLS_OVERHEAD_TOKENS = 10_000                       # tool schemas, not visible to the callback
CACHE_WRITE_EXTRA = 0.25
DEFAULT_CEILING_USD = 5.0


class BudgetExceeded(RuntimeError):
    pass


def cost_usd(input_tokens: int, output_tokens: int, cache_write_tokens: int = 0) -> float:
    mult = LONG_PROMPT_MULT if input_tokens > LONG_PROMPT_TOKENS else 1
    return mult * ((input_tokens + CACHE_WRITE_EXTRA * cache_write_tokens) * IN_USD_PER_M
                   + output_tokens * OUT_USD_PER_M) / 1e6


def ceiling_from_env() -> float:
    try:
        v = float(os.getenv("SPEND_CEILING_USD", ""))
        return v if v >= 0 else DEFAULT_CEILING_USD
    except ValueError:
        return DEFAULT_CEILING_USD


class Budget(BaseCallbackHandler):
    raise_error = True   # an exception here must stop the model call, not be logged and ignored

    def __init__(self, store=None, ceiling_usd: float | None = None):
        self.store = store                # EventStore (ledger survives restarts) or None
        self.ceiling = ceiling_from_env() if ceiling_usd is None else ceiling_usd
        self.event_id = None              # set by whoever starts a run, for the ledger
        self._lock = threading.Lock()
        self._mem = 0.0                   # ledger when there is no store
        self.calls = self.input_tokens = self.output_tokens = 0
        self.run_cost = 0.0
        self.anomalies: list[str] = []    # model answers that were cut off or empty (any agent)

    def start_run(self, event_id=None):
        with self._lock:
            self.event_id = event_id
            self.calls = self.input_tokens = self.output_tokens = 0
            self.run_cost = 0.0
            self.anomalies = []

    def spent(self) -> float:
        return self.store.total_spend() if self.store is not None else self._mem

    def exhausted(self) -> bool:
        return self.spent() >= self.ceiling

    def on_chat_model_start(self, serialized, messages, **kwargs):
        if self.exhausted():
            raise BudgetExceeded(f"spend ${self.spent():.4f} reached the ${self.ceiling:.2f} ceiling")
        est = TOOLS_OVERHEAD_TOKENS + sum(len(str(m.content)) for b in messages for m in b) // 3
        if est > MAX_PROMPT_TOKENS:
            raise BudgetExceeded(f"prompt of about {est} tokens is over {MAX_PROMPT_TOKENS}")

    def on_llm_end(self, response, **kwargs):
        try:
            msg = response.generations[0][0].message
            usage = msg.usage_metadata or {}
            stop = (msg.response_metadata or {}).get("stop_reason")
            empty = not (msg.text or "").strip() and not msg.tool_calls
            if stop == "max_tokens" or empty:
                self.anomalies.append(
                    f"model answer {'cut off at the token limit' if stop == 'max_tokens' else 'empty'}"
                    f" (stop_reason={stop}, output_tokens={usage.get('output_tokens')}, "
                    f"invalid_tool_calls={len(msg.invalid_tool_calls or [])})")
        except (AttributeError, IndexError):
            usage = {}
        i, o = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cw = int((usage.get("input_token_details") or {}).get("cache_creation", 0) or 0)
        c = cost_usd(i, o, cw)
        with self._lock:
            self.calls += 1
            self.input_tokens += i
            self.output_tokens += o
            self.run_cost += c
            self._mem += c
        if self.store is not None:
            self.store.add_spend(self.event_id, i, o, c)

    def summary(self) -> str:
        return (f"{self.calls} model calls, {self.input_tokens} in / {self.output_tokens} out "
                f"tokens, ${self.run_cost:.4f} this run, ${self.spent():.4f} total "
                f"(ceiling ${self.ceiling:.2f})")
