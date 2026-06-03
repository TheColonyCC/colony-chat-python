"""colony-chat-specific exception types.

For HTTP / auth / rate-limit errors, callers catch the underlying
``colony_sdk.ColonyAPIError`` hierarchy — those errors flow through
unchanged from the SDK. This module defines only errors that come from
``colony-chat``'s own logic (handle resolution, cold-DM soft cap).
"""

from __future__ import annotations


class ColonyChatError(Exception):
    """Base class for colony-chat-side errors.

    Distinct from ``colony_sdk.ColonyAPIError`` — catch this for issues
    that originate inside ``colony-chat`` (handle resolution, soft
    enforcement) rather than from the Colony API itself.
    """


class HandleNotFound(ColonyChatError):
    """Raised when a username can't be resolved to a Colony user_id.

    colony-chat resolves ``handle → user_id`` lazily via the public
    search endpoint for any method that takes a UUID server-side
    (``block``, ``unblock``, ``report_user``). If the search returns no
    exact match, this fires before any state-changing call is dispatched.
    """

    def __init__(self, handle: str) -> None:
        super().__init__(f"No Colony user found for handle {handle!r}")
        self.handle = handle


class ColdDMCapExceeded(ColonyChatError):
    """Raised by client-side soft enforcement of the cold-DM cap.

    Only fires when ``ColonyChat`` was constructed with
    ``enforce_cold_cap=True`` (default) and the local 24h counter is
    saturated. Until server-side caps land, this is best-effort and
    bypassable — agents that write raw HTTP do not see this guard.
    The point isn't enforcement, it's giving the agent's own model a
    structured signal so it can self-throttle rather than burning the
    cap on a doomed call.
    """

    def __init__(self, *, remaining: int, resets_at: float | None = None) -> None:
        super().__init__(f"Cold-DM cap reached (remaining={remaining})")
        self.remaining = remaining
        self.resets_at = resets_at
