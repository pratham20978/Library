"""Turning failures into something an operator can act on (arch §45).

The MCP SDK builds every transport on anyio task groups, so a failed upstream
handshake does not arrive as "401 Unauthorized" — it arrives as an
`ExceptionGroup` whose own text is "unhandled errors in a TaskGroup
(1 sub-exception)". That string is accurate and completely useless, and it is
what would otherwise land in a health report.

`describe_exception` is the fix. These tests pin the two things it must do: dig
out the leaves, and stay bounded so a group of thirty identical failures does not
become the audit row.
"""

from __future__ import annotations

import pytest

from app.core.errors import (
    HubError,
    IntegrationNotFound,
    NotAuthorized,
    RateLimited,
    UpstreamError,
    describe_exception,
)

pytestmark = pytest.mark.security


# ------------------------------------------------------------- exception group text


def test_a_plain_exception_describes_itself() -> None:
    assert describe_exception(ValueError("bad input")) == "ValueError: bad input"


def test_a_group_reports_its_leaf_not_its_own_text() -> None:
    """The whole reason this function exists."""
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionRefusedError("connection refused")])

    described = describe_exception(group)

    assert "TaskGroup" not in described
    assert described == "ConnectionRefusedError: connection refused"


def test_nested_groups_are_flattened() -> None:
    """The SDK nests them two and three deep in practice."""
    inner = ExceptionGroup("inner", [TimeoutError("upstream timed out")])
    outer = ExceptionGroup("outer", [inner])

    assert describe_exception(outer) == "TimeoutError: upstream timed out"


def test_several_distinct_leaves_are_all_named() -> None:
    group = ExceptionGroup(
        "group",
        [ConnectionRefusedError("refused"), TimeoutError("timed out")],
    )

    described = describe_exception(group)

    assert "refused" in described
    assert "timed out" in described


def test_a_large_group_is_summarised_rather_than_dumped() -> None:
    """An audit row stays a row. Thirty identical DNS failures are one line."""
    group = ExceptionGroup("group", [ConnectionRefusedError(f"host {n}") for n in range(30)])

    described = describe_exception(group, limit=3)

    assert described.count("ConnectionRefusedError") == 3
    assert "(+27 more)" in described


def test_limit_is_honoured_exactly() -> None:
    group = ExceptionGroup("group", [ValueError(str(n)) for n in range(5)])

    assert "(+4 more)" in describe_exception(group, limit=1)
    assert "more)" not in describe_exception(group, limit=5)


# -------------------------------------------------------------------- message shapes


def test_an_error_code_is_appended_when_the_exception_carries_one() -> None:
    """The SDK renders every JSON-RPC failure with the same sentence, so the
    code is the only thing distinguishing "invalid params" from "internal
    error" — and it is what an operator will search for."""

    class Coded(Exception):
        code = -32603

    described = describe_exception(Coded("Server returned an error response"))

    assert "[code -32603]" in described


def test_a_code_already_present_in_the_message_is_not_repeated() -> None:
    class Coded(Exception):
        code = -32603

    described = describe_exception(Coded("failed with code -32603"))

    assert described.count("-32603") == 1


def test_an_empty_message_falls_back_to_the_cause() -> None:
    """A bare `raise X from Y` still says something useful."""
    try:
        try:
            raise ConnectionResetError("peer went away")
        except ConnectionResetError as cause:
            raise RuntimeError from cause
    except RuntimeError as exc:
        described = describe_exception(exc)

    assert "peer went away" in described
    assert "RuntimeError" in described


def test_an_exception_with_no_text_at_all_still_names_its_type() -> None:
    assert describe_exception(KeyboardInterrupt()) == "KeyboardInterrupt"


# ------------------------------------------------------------------- the hierarchy


def test_hub_errors_carry_a_stable_code_and_status() -> None:
    """The REST layer and the MCP error path both read these, so they are API."""
    assert IntegrationNotFound("no such thing").http_status == 404
    assert NotAuthorized("nope").http_status == 403
    assert RateLimited("slow down").http_status == 429


def test_the_wire_payload_carries_the_details_verbatim() -> None:
    payload = IntegrationNotFound("Unknown integration 'ghost'.", integration="ghost").to_payload()

    assert payload["code"] == "integration_not_found"
    assert payload["message"] == "Unknown integration 'ghost'."
    assert payload["details"] == {"integration": "ghost"}


def test_a_payload_without_details_omits_the_key() -> None:
    """An empty `details: {}` in every response is noise a client has to skip."""
    assert "details" not in HubError("something went wrong").to_payload()


def test_every_hub_error_is_catchable_as_the_base_class() -> None:
    """One `except HubError` in the API layer has to cover all of them."""
    for error in (
        IntegrationNotFound("a"),
        NotAuthorized("b"),
        RateLimited("c"),
        UpstreamError("d"),
    ):
        assert isinstance(error, HubError)
