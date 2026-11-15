"""Shared test setup: the policy's memory (sources, write rates, recent setting values) is
module state, so every test starts with it empty."""
import pytest

from gateway import policy


@pytest.fixture(autouse=True)
def clean_policy_memory():
    policy.reset_context()
    yield
    policy.reset_context()
