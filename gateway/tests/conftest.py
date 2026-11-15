"""Shared test setup: the policy's memory (sources, write rates, recent setting values) is
module state, so every test starts with it empty. The two addresses the tests send from
(this machine and test_policy.SRC) are operators unless a test sets OPERATOR_IPS itself, so
the developer's .env does not change a result."""
import pytest

from gateway import policy


@pytest.fixture(autouse=True)
def clean_policy_memory(monkeypatch):
    monkeypatch.setenv("OPERATOR_IPS", "127.0.0.1,203.0.113.7")
    policy.reset_context()
    yield
    policy.reset_context()
