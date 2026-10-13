import pytest


@pytest.fixture(autouse=True)
def no_publish_hook_from_the_caller(monkeypatch):
    """The developer's own VERDICT_PUBLISH_HOOK must never run for a test review."""
    monkeypatch.delenv("VERDICT_PUBLISH_HOOK", raising=False)
