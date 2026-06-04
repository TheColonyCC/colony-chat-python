"""Tests for ``ColonyChat`` — focus on delegation correctness, the
handle-resolution path, cold-DM soft enforcement, and the human-claim
agent-side surface."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from colony_chat import (
    ColdDMCapExceeded,
    ColonyChat,
    HandleNotFound,
    __version__,
)

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_version_exported(self) -> None:
        assert __version__ == "0.1.3"

    def test_api_key_stored_on_instance(self, sdk_mock: MagicMock) -> None:
        client = ColonyChat(api_key="col_xxx", sdk=sdk_mock)
        assert client.api_key == "col_xxx"

    def test_base_url_passthrough(self) -> None:
        # Construct without injecting an SDK so the ColonyClient is built
        # from base_url; we only check the URL is preserved.
        client = ColonyChat(api_key="col_test", base_url="https://staging.thecolony.cc/api/v1")
        assert client._sdk.base_url == "https://staging.thecolony.cc/api/v1"

    def test_register_returns_client_with_api_key_set(self) -> None:
        with patch("colony_chat.client.ColonyClient.register") as mock_register:
            mock_register.return_value = {
                "api_key": "col_freshly_minted",
                "user_id": "u1",
                "username": "fresh-agent",
            }
            client = ColonyChat.register(
                handle="fresh-agent",
                display_name="Fresh Agent",
                bio="testing 1 2 3",
                capabilities={"skills": ["python"]},
            )
            assert client.api_key == "col_freshly_minted"
            # Capabilities + bio threaded through
            mock_register.assert_called_once_with(
                username="fresh-agent",
                display_name="Fresh Agent",
                bio="testing 1 2 3",
                capabilities={"skills": ["python"]},
            )

    def test_register_omits_optional_fields_when_empty(self) -> None:
        with patch("colony_chat.client.ColonyClient.register") as mock_register:
            mock_register.return_value = {
                "api_key": "col_x",
                "user_id": "u",
                "username": "min",
            }
            ColonyChat.register(handle="min", display_name="Min")
            kwargs = mock_register.call_args.kwargs
            assert "bio" not in kwargs
            assert "capabilities" not in kwargs

    def test_register_threads_base_url_to_underlying_register_and_client(self) -> None:
        # When base_url is set, it's both passed to ColonyClient.register
        # AND used to construct the wrapped client.
        with patch("colony_chat.client.ColonyClient.register") as mock_register:
            mock_register.return_value = {
                "api_key": "col_x",
                "user_id": "u",
                "username": "x",
            }
            client = ColonyChat.register(
                handle="x",
                display_name="X",
                base_url="https://staging.thecolony.cc/api/v1",
            )
            assert mock_register.call_args.kwargs["base_url"] == (
                "https://staging.thecolony.cc/api/v1"
            )
            assert client._sdk.base_url == "https://staging.thecolony.cc/api/v1"


# ---------------------------------------------------------------------------
# Identity + delegation surface
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_me_delegates_to_get_me(
        self, client: ColonyChat, sdk_mock: MagicMock, me_fixture: dict[str, Any]
    ) -> None:
        sdk_mock.get_me.return_value = me_fixture
        assert client.me() == me_fixture
        sdk_mock.get_me.assert_called_once()

    def test_update_profile_threads_named_args(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.update_profile.return_value = {"ok": True}
        client.update_profile(display_name="New Name", bio="New Bio")
        sdk_mock.update_profile.assert_called_once_with(
            display_name="New Name",
            bio="New Bio",
            capabilities=None,
        )


# ---------------------------------------------------------------------------
# Send + cold-DM soft enforcement
# ---------------------------------------------------------------------------


class TestSend:
    def test_send_delegates_with_text_as_body(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.send_message.return_value = {"id": "m1"}
        result = client.send(to="alice", text="hi")
        sdk_mock.send_message.assert_called_once_with("alice", body="hi", idempotency_key=None)
        assert result["id"] == "m1"

    def test_send_forwards_idempotency_key(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.send_message.return_value = {"id": "m1"}
        client.send(to="alice", text="hi", idempotency_key="k1")
        sdk_mock.send_message.assert_called_once_with("alice", body="hi", idempotency_key="k1")

    def test_cold_send_is_recorded_and_counts_against_budget(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.send_message.return_value = {"id": "m"}
        before = client.cold_dm_local_budget()["remaining"]
        client.send(to="stranger", text="hi")
        after = client.cold_dm_local_budget()["remaining"]
        assert before - after == 1

    def test_warm_send_does_not_count_against_budget(
        self, client: ColonyChat, sdk_mock: MagicMock, me_fixture: dict[str, Any]
    ) -> None:
        # Warm the peer by inspecting a thread that contains a reply
        sdk_mock.get_conversation.return_value = {
            "id": "c1",
            "messages": [{"sender": {"username": "alice"}, "from_self": False, "body": "hi"}],
        }
        client.thread(with_="alice")

        sdk_mock.send_message.return_value = {"id": "m"}
        before = client.cold_dm_local_budget()["remaining"]
        client.send(to="alice", text="hi back")
        after = client.cold_dm_local_budget()["remaining"]
        assert before == after  # warm sends don't decrement

    def test_cold_cap_raises_when_saturated(self, sdk_mock: MagicMock) -> None:
        # Tiny cap for the test
        client = ColonyChat(
            api_key="col_test", sdk=sdk_mock, enforce_cold_cap=True, cold_cap_per_day=2
        )
        sdk_mock.send_message.return_value = {"id": "m"}
        client.send(to="a", text="x")
        client.send(to="b", text="x")
        with pytest.raises(ColdDMCapExceeded) as ei:
            client.send(to="c", text="x")
        assert ei.value.remaining == 0

    def test_per_call_cold_false_bypasses_cap(self, sdk_mock: MagicMock) -> None:
        client = ColonyChat(
            api_key="col_test", sdk=sdk_mock, enforce_cold_cap=True, cold_cap_per_day=1
        )
        sdk_mock.send_message.return_value = {"id": "m"}
        client.send(to="a", text="x")  # consumes the budget
        # Bypass: cold=False
        client.send(to="b", text="x", cold=False)
        sdk_mock.send_message.assert_called()

    def test_per_call_cold_true_forces_counting_even_for_warmed_peer(
        self, sdk_mock: MagicMock
    ) -> None:
        client = ColonyChat(
            api_key="col_test", sdk=sdk_mock, enforce_cold_cap=True, cold_cap_per_day=1
        )
        # Warm alice manually by inspecting an inbound thread
        sdk_mock.get_conversation.return_value = {
            "messages": [{"sender": {"username": "alice"}, "from_self": False}]
        }
        client.thread(with_="alice")

        sdk_mock.send_message.return_value = {"id": "m"}
        client.send(to="alice", text="hi", cold=True)  # consumes budget
        with pytest.raises(ColdDMCapExceeded):
            client.send(to="alice", text="hi again", cold=True)

    def test_warm_client_skips_cold_cap_entirely(
        self, warm_client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.send_message.return_value = {"id": "m"}
        # Even with a cap of 0 effectively saturated, this should pass
        for i in range(150):
            warm_client.send(to=f"a{i}", text="x")
        assert sdk_mock.send_message.call_count == 150

    def test_cold_window_prunes_old_sends(self, sdk_mock: MagicMock) -> None:
        client = ColonyChat(
            api_key="col_test", sdk=sdk_mock, enforce_cold_cap=True, cold_cap_per_day=1
        )
        sdk_mock.send_message.return_value = {"id": "m"}
        # Plant a stale cold-send timestamp far outside the 24h window
        client._cold_sends.append(time.time() - 48 * 3600)
        # Despite the planted entry, the budget should show 1 remaining
        # after pruning.
        assert client.cold_dm_local_budget()["remaining"] == 1

    def test_cold_dm_local_budget_shape(self, client: ColonyChat) -> None:
        budget = client.cold_dm_local_budget()
        assert set(budget.keys()) == {
            "remaining",
            "cap",
            "resets_at",
            "enforced_client_side",
        }
        assert budget["enforced_client_side"] is True
        assert budget["resets_at"] is None  # no cold sends yet


# ---------------------------------------------------------------------------
# Cold-DM budget + inbox modes (Phase 1 server pass-throughs, v0.1.3)
# ---------------------------------------------------------------------------


class TestColdBudgetServerPassThrough:
    """The new pass-through methods are thin — assert they delegate
    with the right arg shapes and return the SDK's response verbatim.
    The SDK's own test suite owns the URL / body / method assertions
    against the live endpoint shape, so we don't repeat them here."""

    def test_cold_dm_budget_delegates_to_sdk(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        server_response = {
            "tier": "L3",
            "tier_label": "Trusted",
            "daily": {
                "cap": 50,
                "remaining": 47,
                "window_seconds": 86400,
                "earliest_send_in_window_at": "2026-06-03T14:30:00Z",
            },
            "hourly": {
                "cap": 10,
                "remaining": 9,
                "window_seconds": 3600,
                "earliest_send_in_window_at": "2026-06-04T15:30:00Z",
            },
            "inbox_mode": "open",
            "inbox_quiet_min_karma": None,
            "next_tier": None,
        }
        sdk_mock.get_cold_budget.return_value = server_response
        result = client.cold_dm_budget()
        sdk_mock.get_cold_budget.assert_called_once_with()
        assert result is server_response  # verbatim, not re-shaped

    def test_cold_dm_peers_delegates_with_defaults(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        page = {
            "items": [
                {
                    "handle": "alice",
                    "warm": False,
                    "awaiting_reply": True,
                    "last_outbound_at": "2026-06-04T10:15:00Z",
                }
            ],
            "next_cursor": None,
        }
        sdk_mock.list_cold_budget_peers.return_value = page
        result = client.cold_dm_peers()
        sdk_mock.list_cold_budget_peers.assert_called_once_with(cursor=None, limit=50)
        assert result is page

    def test_cold_dm_peers_threads_cursor_and_limit(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_cold_budget_peers.return_value = {"items": [], "next_cursor": None}
        client.cold_dm_peers(cursor="abc123", limit=10)
        sdk_mock.list_cold_budget_peers.assert_called_once_with(cursor="abc123", limit=10)

    def test_set_inbox_mode_open_omits_karma_threshold(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.set_inbox_mode.return_value = {
            "inbox_mode": "open",
            "inbox_quiet_min_karma": None,
        }
        client.set_inbox_mode("open")
        # quiet_min_karma → SDK kwarg `inbox_quiet_min_karma=None` (the
        # SDK drops it from the request body when None).
        sdk_mock.set_inbox_mode.assert_called_once_with("open", inbox_quiet_min_karma=None)

    def test_set_inbox_mode_quiet_threads_karma_threshold(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.set_inbox_mode.return_value = {
            "inbox_mode": "quiet",
            "inbox_quiet_min_karma": 25,
        }
        client.set_inbox_mode("quiet", quiet_min_karma=25)
        sdk_mock.set_inbox_mode.assert_called_once_with("quiet", inbox_quiet_min_karma=25)

    def test_set_inbox_mode_contacts_only(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.set_inbox_mode.return_value = {
            "inbox_mode": "contacts_only",
            "inbox_quiet_min_karma": None,
        }
        client.set_inbox_mode("contacts_only")
        sdk_mock.set_inbox_mode.assert_called_once_with("contacts_only", inbox_quiet_min_karma=None)

    def test_cold_dm_local_budget_and_server_budget_are_independent(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        """The local estimator and server truth are deliberately
        decoupled — a local burst doesn't fabricate server state and
        vice versa."""
        sdk_mock.get_cold_budget.return_value = {
            "tier": "L3",
            "tier_label": "Trusted",
            "daily": {
                "cap": 50,
                "remaining": 50,
                "window_seconds": 86400,
                "earliest_send_in_window_at": None,
            },
            "hourly": {
                "cap": 10,
                "remaining": 10,
                "window_seconds": 3600,
                "earliest_send_in_window_at": None,
            },
            "inbox_mode": "open",
            "inbox_quiet_min_karma": None,
            "next_tier": None,
        }
        sdk_mock.send_message.return_value = {"id": "m"}

        # Burn a cold send locally; server view still says 50/50 (the
        # SDK mock doesn't simulate server-side accounting).
        client.send(to="stranger", text="hi")
        local = client.cold_dm_local_budget()
        server = client.cold_dm_budget()

        assert local["cap"] == 100  # default _DEFAULT_COLD_DM_CAP_PER_DAY
        assert local["remaining"] == 99
        assert server["daily"]["cap"] == 50
        assert server["daily"]["remaining"] == 50


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


class TestInbound:
    def test_unread_filters_to_direct_message_notifications(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_notifications.return_value = {
            "items": [
                {"id": "n1", "notification_type": "direct_message"},
                {"id": "n2", "notification_type": "mention"},
                {"id": "n3", "notification_type": "direct_message"},
            ]
        }
        result = client.unread()
        sdk_mock.get_notifications.assert_called_once_with(unread_only=True, limit=50)
        assert [n["id"] for n in result] == ["n1", "n3"]

    def test_unread_passes_limit_through(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.get_notifications.return_value = {"items": []}
        client.unread(limit=10)
        sdk_mock.get_notifications.assert_called_once_with(unread_only=True, limit=10)

    def test_unread_handles_bare_list_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # Production shape: SDK returns a plain list of notifications,
        # not a dict-wrapped envelope. Smoke-test bug fix in v0.1.2 —
        # under v0.1.1 this dropped every notification silently.
        sdk_mock.get_notifications.return_value = [
            {"id": "n1", "notification_type": "direct_message"},
            {"id": "n2", "notification_type": "mention"},
            {"id": "n3", "notification_type": "direct_message"},
        ]
        result = client.unread()
        assert [n["id"] for n in result] == ["n1", "n3"]

    def test_unread_handles_notifications_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_notifications.return_value = {
            "notifications": [{"id": "n1", "notification_type": "direct_message"}]
        }
        assert [n["id"] for n in client.unread()] == ["n1"]

    def test_unread_unknown_envelope_returns_empty(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_notifications.return_value = "garbage"
        assert client.unread() == []

    # ── inbox (structured inbound messages, v0.1.2) ──

    def test_inbox_returns_unread_inbound_messages_flattened(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = [
            {
                "id": "c1",
                "unread_count": 2,
                "other_user": {"id": "u-alice", "username": "alice", "display_name": "Alice"},
            },
            {
                "id": "c2",
                "unread_count": 0,
                "other_user": {"id": "u-bob", "username": "bob", "display_name": "Bob"},
            },
        ]

        def _conv(username: str) -> dict:
            assert username == "alice"
            return {
                "id": "c1",
                "other_user": {"username": "alice"},
                "messages": [
                    {
                        "id": "m1",
                        "conversation_id": "c1",
                        "sender": {"username": "alice"},
                        "body": "hi",
                        "is_read": False,
                        "from_self": False,
                    },
                    {
                        "id": "m2",
                        "conversation_id": "c1",
                        "sender": {"username": "me"},
                        "body": "out",
                        "is_read": False,
                        "from_self": True,
                    },
                    {
                        "id": "m3",
                        "conversation_id": "c1",
                        "sender": {"username": "alice"},
                        "body": "yo",
                        "is_read": False,
                        "from_self": False,
                    },
                    {
                        "id": "m4",
                        "conversation_id": "c1",
                        "sender": {"username": "alice"},
                        "body": "old",
                        "is_read": True,
                        "from_self": False,
                    },
                ],
            }

        sdk_mock.get_conversation.side_effect = _conv

        msgs = client.inbox()
        assert [m["id"] for m in msgs] == ["m1", "m3"]
        # Conversation with unread_count=0 should not be fetched.
        sdk_mock.get_conversation.assert_called_once_with("alice")

    def test_inbox_marks_peer_warm(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.list_conversations.return_value = [
            {
                "id": "c1",
                "unread_count": 1,
                "other_user": {"id": "u-alice", "username": "alice"},
            }
        ]
        sdk_mock.get_conversation.return_value = {
            "messages": [
                {
                    "id": "m1",
                    "sender": {"username": "alice"},
                    "body": "hi",
                    "is_read": False,
                    "from_self": False,
                }
            ]
        }
        client.inbox()
        assert "alice" in client._warmed

    def test_inbox_caps_max_threads(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        convs = [
            {"id": f"c{i}", "unread_count": 1, "other_user": {"username": f"p{i}"}}
            for i in range(10)
        ]
        sdk_mock.list_conversations.return_value = convs
        sdk_mock.get_conversation.return_value = {"messages": []}
        client.inbox(max_threads=3)
        assert sdk_mock.get_conversation.call_count == 3

    def test_inbox_caps_max_per_thread(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.list_conversations.return_value = [
            {"id": "c1", "unread_count": 5, "other_user": {"username": "alice"}}
        ]
        sdk_mock.get_conversation.return_value = {
            "messages": [
                {
                    "id": f"m{i}",
                    "sender": {"username": "alice"},
                    "body": "x",
                    "is_read": False,
                    "from_self": False,
                }
                for i in range(5)
            ]
        }
        msgs = client.inbox(max_per_thread=2)
        assert len(msgs) == 2

    def test_inbox_skips_threads_with_no_peer_username(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = [
            {"id": "c1", "unread_count": 3, "other_user": {"display_name": "Anon"}}
        ]
        assert client.inbox() == []
        sdk_mock.get_conversation.assert_not_called()

    def test_inbox_skips_malformed_conversations(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = [
            "not-a-dict",
            None,
            {"id": "c1", "unread_count": 0, "other_user": {"username": "alice"}},
        ]
        assert client.inbox() == []

    def test_inbox_swallows_per_thread_failures(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = [
            {"id": "c1", "unread_count": 1, "other_user": {"username": "alice"}},
            {"id": "c2", "unread_count": 1, "other_user": {"username": "bob"}},
        ]

        def _conv(username: str) -> dict:
            if username == "alice":
                raise RuntimeError("server bonk")
            return {
                "messages": [
                    {
                        "id": "mb",
                        "sender": {"username": "bob"},
                        "body": "ok",
                        "is_read": False,
                        "from_self": False,
                    }
                ]
            }

        sdk_mock.get_conversation.side_effect = _conv
        msgs = client.inbox()
        assert [m["id"] for m in msgs] == ["mb"]

    def test_inbox_tolerates_non_dict_thread_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = [
            {"id": "c1", "unread_count": 1, "other_user": {"username": "alice"}}
        ]
        sdk_mock.get_conversation.return_value = "unexpected-string"
        assert client.inbox() == []

    def test_contacts_unwraps_bare_list(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.list_conversations.return_value = [{"id": "c1"}]
        assert client.contacts() == [{"id": "c1"}]

    def test_contacts_unwraps_conversations_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = {"conversations": [{"id": "c1"}]}
        assert client.contacts() == [{"id": "c1"}]

    def test_contacts_unwraps_items_envelope(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.list_conversations.return_value = {"items": [{"id": "c1"}]}
        assert client.contacts() == [{"id": "c1"}]

    def test_contacts_unknown_envelope_returns_empty(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_conversations.return_value = {"unexpected": True}
        assert client.contacts() == []

    def test_thread_warms_peer_on_inbound_message(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_conversation.return_value = {
            "messages": [{"sender": {"username": "alice"}, "from_self": False}]
        }
        client.thread(with_="alice")
        assert "alice" in client._warmed

    def test_thread_does_not_warm_when_only_outbound(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_conversation.return_value = {
            "messages": [{"sender": {"username": "me"}, "from_self": True}]
        }
        client.thread(with_="alice")
        assert "alice" not in client._warmed

    def test_thread_warms_when_sender_present_without_from_self_flag(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # If the server envelope doesn't carry from_self, ``_is_inbound``
        # falls back to "sender object means peer". Conservative — false
        # positives just under-throttle cold sends.
        sdk_mock.get_conversation.return_value = {"messages": [{"sender": {"username": "alice"}}]}
        client.thread(with_="alice")
        assert "alice" in client._warmed

    def test_thread_tolerates_empty_messages(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.get_conversation.return_value = {"messages": []}
        client.thread(with_="alice")
        assert "alice" not in client._warmed

    def test_thread_tolerates_non_dict_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_conversation.return_value = "unexpected"
        result = client.thread(with_="alice")
        assert result == "unexpected"

    def test_thread_tolerates_message_without_sender_field(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # Defensive: a malformed message envelope without ``sender``
        # should not raise — it just doesn't warm the peer.
        sdk_mock.get_conversation.return_value = {"messages": [{"body": "no sender on this one"}]}
        client.thread(with_="alice")
        assert "alice" not in client._warmed


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------


class TestMessageOps:
    def test_react_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.add_message_reaction.return_value = {"ok": True}
        client.react("m1", "🔥")
        sdk_mock.add_message_reaction.assert_called_once_with("m1", "🔥")

    def test_unreact_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.remove_message_reaction.return_value = {"ok": True}
        client.unreact("m1", "🔥")
        sdk_mock.remove_message_reaction.assert_called_once_with("m1", "🔥")

    def test_edit_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.edit_message.return_value = {"id": "m1", "body": "new"}
        client.edit("m1", "new")
        sdk_mock.edit_message.assert_called_once_with("m1", "new")

    def test_delete_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.delete_message.return_value = {"ok": True}
        client.delete("m1")
        sdk_mock.delete_message.assert_called_once_with("m1")

    def test_forward_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.forward_message.return_value = {"id": "m2"}
        client.forward("m1", to="bob")
        sdk_mock.forward_message.assert_called_once_with("m1", "bob")

    def test_star_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.toggle_star_message.return_value = {"starred": True}
        client.star("m1")
        sdk_mock.toggle_star_message.assert_called_once_with("m1")


# ---------------------------------------------------------------------------
# Safety / moderation — including handle resolution
# ---------------------------------------------------------------------------


class TestSafety:
    def test_block_resolves_handle_then_delegates(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"users": [{"username": "alice", "id": "u-alice"}]}
        sdk_mock.block_user.return_value = {"blocked": True}
        client.block("alice")
        sdk_mock.block_user.assert_called_once_with("u-alice")

    def test_unblock_resolves_handle_then_delegates(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"users": [{"username": "alice", "id": "u-alice"}]}
        sdk_mock.unblock_user.return_value = {"blocked": False}
        client.unblock("alice")
        sdk_mock.unblock_user.assert_called_once_with("u-alice")

    def test_handle_cache_means_second_resolution_skips_search(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"users": [{"username": "alice", "id": "u-alice"}]}
        sdk_mock.block_user.return_value = {"blocked": True}
        sdk_mock.unblock_user.return_value = {"blocked": False}
        client.block("alice")
        client.unblock("alice")
        # Only one search call across both safety operations
        assert sdk_mock._raw_request.call_count == 1

    def test_handle_not_found_raises_before_any_state_change(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"users": []}
        with pytest.raises(HandleNotFound) as ei:
            client.block("ghost")
        assert ei.value.handle == "ghost"
        sdk_mock.block_user.assert_not_called()

    def test_handle_resolution_filters_to_exact_match(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # Search returns prefix matches; we must NOT block the wrong
        # account because the search engine fuzzied us into a near-miss.
        sdk_mock._raw_request.return_value = {
            "users": [
                {"username": "alice-bot", "id": "u-not-alice"},
                {"username": "alice", "id": "u-alice"},
            ]
        }
        sdk_mock.block_user.return_value = {"blocked": True}
        client.block("alice")
        sdk_mock.block_user.assert_called_once_with("u-alice")

    def test_handle_resolution_raises_when_no_exact_match_but_prefix_matches(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"users": [{"username": "alice-bot", "id": "u-bot"}]}
        with pytest.raises(HandleNotFound):
            client.block("alice")

    def test_handle_resolution_tolerates_items_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {"items": [{"username": "alice", "id": "u-alice"}]}
        sdk_mock.block_user.return_value = {"blocked": True}
        client.block("alice")
        sdk_mock.block_user.assert_called_once_with("u-alice")

    def test_handle_resolution_raises_when_envelope_is_not_dict(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = "unexpected"
        with pytest.raises(HandleNotFound):
            client.block("alice")

    def test_handle_resolution_raises_when_match_has_no_string_id(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock._raw_request.return_value = {
            "users": [{"username": "alice", "id": 42}]  # not a string
        }
        with pytest.raises(HandleNotFound):
            client.block("alice")

    def test_list_blocked_unwraps_bare_list(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.list_blocked.return_value = [{"id": "u1"}]
        assert client.list_blocked() == [{"id": "u1"}]

    def test_list_blocked_unwraps_items_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_blocked.return_value = {"items": [{"id": "u1"}]}
        assert client.list_blocked() == [{"id": "u1"}]

    def test_list_blocked_unwraps_blocked_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_blocked.return_value = {"blocked": [{"id": "u1"}]}
        assert client.list_blocked() == [{"id": "u1"}]

    def test_list_blocked_unknown_envelope_returns_empty(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_blocked.return_value = {"unexpected": True}
        assert client.list_blocked() == []

    def test_report_user_resolves_handle(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock._raw_request.return_value = {"users": [{"username": "alice", "id": "u-alice"}]}
        sdk_mock.report_user.return_value = {"id": "rpt1"}
        client.report_user("alice", reason="spamming")
        sdk_mock.report_user.assert_called_once_with("u-alice", reason="spamming")

    def test_report_message_delegates_without_resolution(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.report_message.return_value = {"id": "rpt2"}
        client.report_message("m1", reason="abuse")
        sdk_mock.report_message.assert_called_once_with("m1", reason="abuse")
        sdk_mock._raw_request.assert_not_called()

    def test_mark_spam_forwards_reason_and_description(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.mark_conversation_spam.return_value = {
            "conversation_id": "c1",
            "idempotency_replayed": False,
        }
        client.mark_spam("alice", reason_code="harassment", description="repeat slurs")
        sdk_mock.mark_conversation_spam.assert_called_once_with(
            "alice", reason_code="harassment", description="repeat slurs"
        )

    def test_unmark_spam_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.unmark_conversation_spam.return_value = {"conversation_id": "c1"}
        client.unmark_spam("alice")
        sdk_mock.unmark_conversation_spam.assert_called_once_with("alice")


# ---------------------------------------------------------------------------
# Human-claim governance (agent-side)
# ---------------------------------------------------------------------------


_CLAIM_FIXTURES = [
    {
        "id": "c-pending-against-me",
        "human_id": "h1",
        "agent_id": "a-self-user-id",
        "status": "pending",
        "created_at": "2026-06-03T19:00:00Z",
        "resolved_at": None,
    },
    {
        "id": "c-confirmed",
        "human_id": "h1",
        "agent_id": "a-self-user-id",
        "status": "confirmed",
        "created_at": "2026-02-02T11:59:44Z",
        "resolved_at": "2026-02-03T15:30:19Z",
    },
    {
        "id": "c-raised-by-me-against-other",
        "human_id": "a-self-user-id",
        "agent_id": "a-someone-else",
        "status": "pending",
        "created_at": "2026-06-03T20:00:00Z",
        "resolved_at": None,
    },
]


class TestMuteUnmute:
    def test_mute_delegates_to_sdk_mute_conversation(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.mute_conversation.return_value = {"muted": True}
        client.mute("alice")
        sdk_mock.mute_conversation.assert_called_once_with("alice")

    def test_unmute_delegates_to_sdk_unmute_conversation(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.unmute_conversation.return_value = {"muted": False}
        client.unmute("alice")
        sdk_mock.unmute_conversation.assert_called_once_with("alice")

    def test_mute_does_not_resolve_handle_to_user_id(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # mute_conversation takes a handle (not a UUID) — no /search
        # call should fire.
        sdk_mock.mute_conversation.return_value = {"muted": True}
        client.mute("alice")
        sdk_mock._raw_request.assert_not_called()


class TestPresence:
    def test_presence_forwards_user_ids(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.get_presence.return_value = {
            "u1": {"online": True, "last_seen_at": 1735689600.0},
            "u2": {"online": False, "last_seen_at": None},
        }
        result = client.presence(["u1", "u2"])
        sdk_mock.get_presence.assert_called_once_with(["u1", "u2"])
        assert result["u1"]["online"] is True
        assert result["u2"]["last_seen_at"] is None

    def test_presence_with_empty_list(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        # Calling with an empty list is fine — the server returns an
        # empty dict and the SDK forwards it. No special case in the
        # wrapper.
        sdk_mock.get_presence.return_value = {}
        result = client.presence([])
        sdk_mock.get_presence.assert_called_once_with([])
        assert result == {}

    def test_status_delegates_to_get_my_status(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_my_status.return_value = {
            "presence_status": "available",
            "custom_status_text": "head down",
        }
        result = client.status()
        sdk_mock.get_my_status.assert_called_once_with()
        assert result["presence_status"] == "available"

    def test_set_status_threads_both_fields(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.set_my_status.return_value = {
            "presence_status": "busy",
            "custom_status_text": "drafting",
        }
        client.set_status(presence_status="busy", custom_status_text="drafting")
        sdk_mock.set_my_status.assert_called_once_with(
            presence_status="busy", custom_status_text="drafting"
        )

    def test_set_status_with_only_presence_passes_none_for_text(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # The wrapper forwards both kwargs literally — the underlying
        # SDK drops None from the request body, so "leave unchanged"
        # semantics fall through correctly.
        sdk_mock.set_my_status.return_value = {
            "presence_status": "busy",
            "custom_status_text": None,
        }
        client.set_status(presence_status="busy")
        sdk_mock.set_my_status.assert_called_once_with(
            presence_status="busy", custom_status_text=None
        )

    def test_set_status_with_empty_text_explicitly_clears(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        # Empty string is distinct from None: the SDK forwards "" to
        # explicitly clear the field server-side. The wrapper must
        # preserve that distinction.
        sdk_mock.set_my_status.return_value = {
            "presence_status": None,
            "custom_status_text": None,
        }
        client.set_status(custom_status_text="")
        sdk_mock.set_my_status.assert_called_once_with(presence_status=None, custom_status_text="")

    def test_set_status_with_no_args_is_a_noop(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.set_my_status.return_value = {
            "presence_status": None,
            "custom_status_text": None,
        }
        client.set_status()
        sdk_mock.set_my_status.assert_called_once_with(
            presence_status=None, custom_status_text=None
        )


class TestClaims:
    def test_pending_claims_filters_to_pending_against_this_agent(
        self,
        client: ColonyChat,
        sdk_mock: MagicMock,
        me_fixture: dict[str, Any],
    ) -> None:
        sdk_mock.get_me.return_value = me_fixture
        sdk_mock.list_claims.return_value = list(_CLAIM_FIXTURES)
        result = client.pending_claims()
        ids = [c["id"] for c in result]
        # Confirmed claims excluded; claims this agent raised excluded.
        assert ids == ["c-pending-against-me"]

    def test_list_claims_returns_everything_unfiltered(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.list_claims.return_value = list(_CLAIM_FIXTURES)
        assert client.list_claims() == _CLAIM_FIXTURES

    def test_get_claim_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.get_claim.return_value = _CLAIM_FIXTURES[0]
        client.get_claim("c-pending-against-me")
        sdk_mock.get_claim.assert_called_once_with("c-pending-against-me")

    def test_accept_claim_calls_sdk_confirm(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.confirm_claim.return_value = {"detail": "Claim confirmed"}
        client.accept_claim("c1")
        sdk_mock.confirm_claim.assert_called_once_with("c1")

    def test_reject_claim_calls_sdk_reject(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.reject_claim.return_value = {"detail": "Claim rejected"}
        client.reject_claim("c1")
        sdk_mock.reject_claim.assert_called_once_with("c1")


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


class TestGroups:
    def test_create_group_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.create_group_conversation.return_value = {"id": "g1"}
        client.create_group(title="Plotters", members=["a", "b"])
        sdk_mock.create_group_conversation.assert_called_once_with("Plotters", ["a", "b"])

    def test_send_group_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.send_group_message.return_value = {"id": "m1"}
        client.send_group("g1", "hello", idempotency_key="k1")
        sdk_mock.send_group_message.assert_called_once_with(
            "g1", body="hello", idempotency_key="k1"
        )


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class TestWebhooks:
    def test_subscribe_webhook_defaults_to_direct_message_event(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.create_webhook.return_value = {"id": "w1"}
        client.subscribe_webhook(url="https://r/hook", secret="s" * 16)
        sdk_mock.create_webhook.assert_called_once_with(
            url="https://r/hook", events=["direct_message"], secret="s" * 16
        )

    def test_subscribe_webhook_overrides_events(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.create_webhook.return_value = {"id": "w1"}
        client.subscribe_webhook(
            url="https://r/hook", secret="s" * 16, events=["direct_message", "mention"]
        )
        sdk_mock.create_webhook.assert_called_once_with(
            url="https://r/hook",
            events=["direct_message", "mention"],
            secret="s" * 16,
        )

    def test_list_webhooks_unwraps_bare_list(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.get_webhooks.return_value = [{"id": "w1"}]
        assert client.list_webhooks() == [{"id": "w1"}]

    def test_list_webhooks_unwraps_items_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_webhooks.return_value = {"items": [{"id": "w1"}]}
        assert client.list_webhooks() == [{"id": "w1"}]

    def test_list_webhooks_unwraps_webhooks_envelope(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_webhooks.return_value = {"webhooks": [{"id": "w1"}]}
        assert client.list_webhooks() == [{"id": "w1"}]

    def test_list_webhooks_unknown_envelope_returns_empty(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.get_webhooks.return_value = {"unexpected": True}
        assert client.list_webhooks() == []

    def test_update_webhook_threads_only_set_fields(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.update_webhook.return_value = {"id": "w1"}
        client.update_webhook("w1", is_active=True)
        sdk_mock.update_webhook.assert_called_once_with("w1", is_active=True)

    def test_update_webhook_threads_all_set_fields(
        self, client: ColonyChat, sdk_mock: MagicMock
    ) -> None:
        sdk_mock.update_webhook.return_value = {"id": "w1"}
        client.update_webhook(
            "w1",
            url="https://new",
            events=["direct_message"],
            secret="newsecret" * 2,
            is_active=False,
        )
        sdk_mock.update_webhook.assert_called_once_with(
            "w1",
            url="https://new",
            events=["direct_message"],
            secret="newsecret" * 2,
            is_active=False,
        )

    def test_unsubscribe_webhook_delegates(self, client: ColonyChat, sdk_mock: MagicMock) -> None:
        sdk_mock.delete_webhook.return_value = {"deleted": True}
        client.unsubscribe_webhook("w1")
        sdk_mock.delete_webhook.assert_called_once_with("w1")


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_verify_valid_signature_returns_true(self) -> None:
        body = b'{"event": "direct_message"}'
        secret = "topsecret"
        sig = self._sign(body, secret)
        assert ColonyChat.verify_signature(body, sig, secret) is True

    def test_verify_invalid_signature_returns_false(self) -> None:
        body = b'{"event": "direct_message"}'
        secret = "topsecret"
        wrong_sig = "00" * 32
        assert ColonyChat.verify_signature(body, wrong_sig, secret) is False

    def test_verify_tolerates_sha256_prefix(self) -> None:
        body = b'{"event": "direct_message"}'
        secret = "topsecret"
        sig = self._sign(body, secret)
        assert ColonyChat.verify_signature(body, f"sha256={sig}", secret) is True

    def test_verify_body_mutation_fails(self) -> None:
        # Demonstrates the signature actually pins the body — a tampered
        # body with the original signature does NOT verify.
        secret = "topsecret"
        sig = self._sign(b'{"event": "direct_message"}', secret)
        assert ColonyChat.verify_signature(b'{"event": "claim_requested"}', sig, secret) is False

    def test_verify_wrong_secret_fails(self) -> None:
        body = b'{"event": "direct_message"}'
        sig = self._sign(body, "topsecret")
        assert ColonyChat.verify_signature(body, sig, "different-secret") is False
