"""A scripted chat model for tests: no network, no cost."""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def call(name, _id=None, **args):
    """An AIMessage that calls one tool."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                              "id": _id or f"call_{name}_{id(args)}"}])


class FakeModel(BaseChatModel):
    script: list = []         # AIMessages (or Exceptions to raise), used in order
    prompts: list = []        # the message lists the model was sent
    tools_seen: list = []     # tool names offered at the last bind
    usage: dict = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}

    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kwargs):
        self.tools_seen[:] = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict)
                                                           else getattr(t, "__name__", str(t)))
                              for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(list(messages))
        m = self.script.pop(0) if self.script else AIMessage(content="Nothing more to do.")
        if isinstance(m, BaseException):
            raise m
        m.usage_metadata = dict(self.usage)
        return ChatResult(generations=[ChatGeneration(message=m)])
