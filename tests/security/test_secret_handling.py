"""Credentials must not be renderable (arch §21, §24, §31).

The leak paths that matter in practice are not deliberate — they are a `repr` in
a traceback, an f-string in a log line, a URL echoed into an audit row. These
tests close those paths rather than the ones an attacker would have to work for.

Every assertion here is "the secret does not appear", which is a weaker claim
than "the secret is safe" but is the one that can be tested exhaustively.
"""

from __future__ import annotations

import logging
import re

import pytest

from app.core.redaction import (
    REDACTED,
    Secret,
    fingerprint,
    looks_sensitive,
    redact_arguments,
    redact_mapping,
    redact_url,
)

pytestmark = pytest.mark.security

VALUE = "ATATT3xFfGF0-super-secret-atlassian-token"


# ------------------------------------------------------------------------ the wrapper


def test_secret_does_not_render_through_any_string_path() -> None:
    """`str`, `repr`, f-strings and `format` all produce the placeholder.

    These are the four ways a value reaches a log line by accident.
    """
    secret = Secret(VALUE)

    assert str(secret) == REDACTED
    assert f"{secret}" == REDACTED
    assert format(secret, ">40") == REDACTED
    assert "{}".format(secret) == REDACTED  # noqa: UP032 - the .format path is the point
    # `repr` keeps the type name, which is what makes a traceback readable, but
    # the value is still gone.
    assert repr(secret) == f"Secret({REDACTED})"
    assert VALUE not in repr(secret)


def test_secret_reveals_only_on_request() -> None:
    """One named method, so every disclosure is greppable."""
    assert Secret(VALUE).reveal() == VALUE


def test_secret_does_not_compare_equal_to_its_own_plaintext() -> None:
    """A stray `==` against a string is `False`, not a timing oracle."""
    secret = Secret(VALUE)

    assert secret != VALUE
    assert secret == Secret(VALUE)
    assert secret != Secret("something else")


def test_secret_reports_emptiness_without_disclosing_length() -> None:
    assert not Secret("")
    assert Secret(VALUE)
    assert Secret("").is_empty
    assert not Secret(VALUE).is_empty


def test_fingerprint_identifies_without_revealing() -> None:
    """Enough to say *which* credential was used; not enough to reconstruct it."""
    tag = fingerprint(VALUE)

    assert VALUE not in tag
    assert tag == fingerprint(VALUE), "the same credential must fingerprint the same"
    assert tag != fingerprint(VALUE + "x")


def test_secret_survives_logging_intact(caplog: pytest.LogCaptureFixture) -> None:
    """The end-to-end claim: a secret passed to a logger does not reach the log."""
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("credential=%s", Secret(VALUE))

    assert VALUE not in caplog.text
    assert REDACTED in caplog.text


# ---------------------------------------------------------------------- name matching


@pytest.mark.parametrize(
    "name",
    [
        "token",
        "api_key",
        "api-key",
        "apiKey",
        "access_key",
        "private_key",
        "password",
        "authorization",
        "auth_token",
        "session",
        "cookie",
        "client_secret",
        "GITHUB_TOKEN",
        "key",
    ],
)
def test_sensitive_names_are_recognised(name: str) -> None:
    assert looks_sensitive(name), f"{name!r} must be treated as a credential"


@pytest.mark.parametrize(
    "name",
    ["tokenizer_config", "keyboard", "monkey", "sort_key", "cache_key", "jql", "project", "summary"],
)
def test_benign_names_are_not_redacted(name: str) -> None:
    """Over-redaction makes an audit trail useless, which is its own failure."""
    assert not looks_sensitive(name)


# ------------------------------------------------------------------------- arguments


def test_sensitive_arguments_collapse_to_the_placeholder() -> None:
    redacted = redact_arguments({"jql": "project = HUB", "api_key": VALUE})

    assert redacted["jql"] == "project = HUB"
    assert redacted["api_key"] == REDACTED


def test_secret_values_are_redacted_whatever_their_key_is_called() -> None:
    """The wrapper wins even when the field name looks harmless."""
    assert redact_arguments({"harmless": Secret(VALUE)})["harmless"] == REDACTED


def test_nested_structures_are_walked() -> None:
    redacted = redact_arguments({"headers": {"authorization": f"Bearer {VALUE}"}, "n": 1})

    assert redacted["headers"]["authorization"] == REDACTED
    assert redacted["n"] == 1


def test_long_strings_are_truncated_rather_than_dropped() -> None:
    """An audit row must stay a row; one argument cannot flood the trail."""
    redacted = redact_arguments({"body": "x" * 5000})

    assert len(str(redacted["body"])) < 1000
    assert "+4" in str(redacted["body"]), "the truncation must say how much was cut"


def test_long_sequences_are_summarised() -> None:
    redacted = redact_arguments({"items": list(range(100))})

    assert isinstance(redacted["items"], list)
    assert len(redacted["items"]) == 21, "20 items plus the count of what was dropped"


def test_deep_structures_do_not_recurse_forever() -> None:
    """Bounded depth, so hostile input cannot turn redaction into the outage."""
    payload: dict[str, object] = {"level": "bottom"}
    for _ in range(50):
        payload = {"nested": payload}

    redacted = redact_mapping(payload)

    assert redacted, "the call must return rather than blow the stack"


def test_empty_arguments_are_empty() -> None:
    assert redact_arguments(None) == {}
    assert redact_arguments({}) == {}


# ------------------------------------------------------------------------------ URLs


def test_url_userinfo_is_stripped() -> None:
    """`https://user:pw@host` is the classic credential-in-a-log-line."""
    safe = redact_url(f"https://alice:{VALUE}@jira.example.com/rest/api")

    assert VALUE not in safe
    assert "alice" not in safe
    assert "jira.example.com/rest/api" in safe


def test_url_query_values_are_dropped_but_keys_kept() -> None:
    """Keys are diagnostic and are not secrets; values may be either."""
    safe = redact_url(f"https://example.com/search?token={VALUE}&q=hub")

    assert VALUE not in safe
    assert "token=" in safe
    assert "q=hub" in safe


def test_url_port_survives_redaction() -> None:
    """An operator debugging a connection needs the port."""
    assert ":8443" in redact_url("https://internal.example.com:8443/mcp")


def test_malformed_url_redacts_to_the_placeholder() -> None:
    """Unparsable input fails closed rather than being echoed."""
    assert redact_url("http://[") == REDACTED


def test_no_test_value_leaks_through_any_helper() -> None:
    """A sweep, so a new code path cannot quietly reintroduce a leak."""
    rendered = " ".join(
        [
            str(Secret(VALUE)),
            repr(Secret(VALUE)),
            fingerprint(VALUE),
            str(redact_arguments({"token": VALUE, "nested": {"secret": VALUE}})),
            redact_url(f"https://u:{VALUE}@example.com/?api_key={VALUE}"),
        ]
    )

    assert VALUE not in rendered
    assert not re.search(r"ATATT3xFfGF0", rendered)
