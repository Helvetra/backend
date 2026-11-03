"""
Shared model client tests.
Model calls reuse one HTTP client per event loop, so connections to the
provider are kept open between translations. See helvetra/backend#135.
"""

import asyncio

from app.services import translation


async def two_clients():
    """Fetch the shared client twice within one event loop."""
    return translation._get_model_client(), translation._get_model_client()


def test_client_is_reused_within_a_loop():
    first, second = asyncio.run(two_clients())
    assert first is second


def test_new_event_loop_gets_a_new_client():
    """A client bound to a finished loop is not reused (tests use a loop per test)."""
    first, _ = asyncio.run(two_clients())
    second, _ = asyncio.run(two_clients())
    assert first is not second


def test_close_releases_the_client():
    async def use_and_close():
        client = translation._get_model_client()
        await translation.close_model_client()
        return client

    client = asyncio.run(use_and_close())
    assert client.is_closed
    assert translation._model_client is None
