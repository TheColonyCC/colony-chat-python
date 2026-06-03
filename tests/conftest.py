"""Shared fixtures for colony-chat tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from colony_sdk import ColonyClient

from colony_chat import ColonyChat


@pytest.fixture
def sdk_mock() -> MagicMock:
    """A ``MagicMock`` standing in for ``colony_sdk.ColonyClient``.

    Spec-bound to the real SDK so unknown method calls error out
    rather than silently MagicMock-ing past typos.
    """
    return MagicMock(spec=ColonyClient)


@pytest.fixture
def client(sdk_mock: MagicMock) -> ColonyChat:
    """A ColonyChat with the SDK delegated to ``sdk_mock``."""
    return ColonyChat(api_key="col_test", sdk=sdk_mock)


@pytest.fixture
def warm_client(sdk_mock: MagicMock) -> ColonyChat:
    """A ColonyChat with cold-cap enforcement disabled."""
    return ColonyChat(api_key="col_test", sdk=sdk_mock, enforce_cold_cap=False)


@pytest.fixture
def my_user_id() -> str:
    return "a-self-user-id"


@pytest.fixture
def me_fixture(my_user_id: str) -> dict[str, Any]:
    return {
        "id": my_user_id,
        "username": "colonist-one",
        "display_name": "ColonistOne",
        "karma": 100,
    }
