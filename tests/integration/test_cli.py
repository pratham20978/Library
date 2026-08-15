"""The command line as an operator and a script both use it (arch §57, §58, §72).

Two things are being tested. First, the exit-code contract, because `mcp-hub
health` in a deployment pipeline and `mcp-hub doctor` in a pre-flight check are
only useful if a non-zero status means what it should:

    0    the command did what was asked
    1    it ran and the answer was "no"
    2    the command line itself was wrong
    130  a human declined a confirmation

Second, that the CLI reaches the same service layer as everything else (arch
§72) — the assertions read state the API would report, not CLI-private bookkeeping.

Everything runs as a real subprocess against a throwaway configuration
directory, because argument parsing, exit codes and stream handling are precisely
what an in-process call would skip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.integration

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_ABORTED = 130


class Cli:
    """Runs the CLI against one isolated configuration directory."""

    def __init__(self, config_dir: Path, runtime_dir: Path, database_url: str) -> None:
        self.config_dir = config_dir
        self.runtime_dir = runtime_dir
        self._env = {
            **{name: value for name, value in os.environ.items() if not name.startswith("MCP_HUB_")},
            "MCP_HUB_ENV": "test",
            "MCP_HUB_CONFIG_DIR": str(config_dir),
            "MCP_HUB_RUNTIME_DIR": str(runtime_dir),
            "MCP_HUB_DATABASE_URL": database_url,
            "MCP_HUB_LOG_LEVEL": "ERROR",
            "MCP_HUB_AUTH_SECRET": "test-secret-not-a-real-one-0123456789abcdef",
            # The environment provider cannot store anything, so `secrets set`
            # needs the encrypted-database one — the same pairing a real
            # deployment uses.
            # A JSON list, because the setting is a tuple field.
            "MCP_HUB_SECRET_PROVIDERS": '["encrypted_database"]',
            "MCP_HUB_SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            # Rich would otherwise wrap output to the terminal width it guesses.
            "COLUMNS": "200",
            "NO_COLOR": "1",
        }

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "app.cli.commands", *args],
            capture_output=True,
            text=True,
            env=self._env,
            input=stdin,
            timeout=180,
            check=False,
        )

    def json(self, *args: str) -> object:
        """Run with `--json` and parse the payload."""
        result = self.run("--json", *args)
        assert result.returncode in (EXIT_OK, EXIT_FAILURE), result.stderr
        return json.loads(result.stdout)


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    """A CLI bound to an empty, valid configuration."""
    config = tmp_path / "config"
    (config / "manifests").mkdir(parents=True)
    (config / "integrations.yaml").write_text(
        "version: 1\ntool_exposure_mode: full\nintegrations: {}\n", encoding="utf-8"
    )
    (config / "policies.yaml").write_text(
        "version: 1\nrequire_authentication: false\npolicies: {}\n", encoding="utf-8"
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return Cli(config, runtime, f"sqlite+aiosqlite:///{tmp_path / 'hub.db'}")


@pytest.fixture
def add_integration(cli: Cli) -> Callable[[str, str], None]:
    """Write a manifest and switch it on, without going through the network."""

    def _add(integration_id: str, endpoint: str) -> None:
        (cli.config_dir / "manifests" / f"{integration_id}.yaml").write_text(
            textwrap.dedent(f"""\
                id: {integration_id}
                namespace: {integration_id.replace("-", "_")}
                trust: remote_official
                source:
                  type: remote
                  endpoint: {endpoint}
                """),
            encoding="utf-8",
        )
        catalog = cli.config_dir / "integrations.yaml"
        catalog.write_text(
            catalog.read_text(encoding="utf-8").replace(
                "integrations: {}", f"integrations:\n  {integration_id}:\n    enabled: true"
            ),
            encoding="utf-8",
        )

    return _add


# ----------------------------------------------------------------- the exit contract


def test_version_succeeds(cli: Cli) -> None:
    result = cli.run("--version")

    assert result.returncode == EXIT_OK
    assert result.stdout.strip()


def test_help_succeeds_and_lists_the_lifecycle_commands(cli: Cli) -> None:
    """Arch §57's commands must be discoverable without reading the source."""
    result = cli.run("--help")

    assert result.returncode == EXIT_OK
    for command in ("install", "update", "remove", "rollback", "health", "doctor"):
        assert command in result.stdout


def test_an_unknown_command_is_a_usage_error(cli: Cli) -> None:
    result = cli.run("frobnicate")

    assert result.returncode == EXIT_USAGE


def test_an_unknown_flag_is_a_usage_error(cli: Cli) -> None:
    """2, not 1: the command never ran, so it did not fail — it was malformed."""
    result = cli.run("list", "--not-a-flag")

    assert result.returncode == EXIT_USAGE


def test_listing_an_empty_hub_succeeds(cli: Cli) -> None:
    """Nothing configured is a valid answer, not an error."""
    result = cli.run("list")

    assert result.returncode == EXIT_OK


def test_a_malformed_environment_variable_is_explained_not_traced(cli: Cli) -> None:
    """The most likely first-run failure, and the one a traceback serves worst.

    `MCP_HUB_SECRET_PROVIDERS` is a tuple field, so pydantic-settings wants a
    JSON list. Writing the bare name is the natural mistake, and the answer
    someone editing `.env` needs is which variable — not an internal settings
    source in a stack trace.
    """
    cli._env["MCP_HUB_SECRET_PROVIDERS"] = "encrypted_database"

    result = cli.run("list")

    assert result.returncode == EXIT_FAILURE
    assert "Traceback" not in result.stderr
    assert "secret_providers" in result.stderr
    assert ".env.example" in result.stderr


def test_an_unknown_integration_is_a_failure_not_a_usage_error(cli: Cli) -> None:
    """The command line was well-formed; the answer is "no such thing"."""
    result = cli.run("show", "ghost")

    assert result.returncode == EXIT_FAILURE
    assert "ghost" in (result.stderr + result.stdout)


def test_doctor_fails_when_a_check_fails(tmp_path: Path, cli: Cli) -> None:
    """`doctor` doubles as a pre-flight gate, so a FAIL has to be non-zero."""
    cli._env["MCP_HUB_AUTH_SECRET"] = ""

    result = cli.run("doctor")

    assert result.returncode == EXIT_FAILURE
    assert "Auth secret" in result.stdout


def test_health_fails_when_an_enabled_integration_is_unavailable(
    cli: Cli, add_integration: Callable[[str, str], None]
) -> None:
    """The deployment gate. Nothing is listening on port 9, by convention."""
    add_integration("ghost", "http://127.0.0.1:9/mcp")

    result = cli.run("health")

    assert result.returncode == EXIT_FAILURE
    assert "ghost" in result.stdout


def test_declining_a_confirmation_exits_130(cli: Cli, add_integration: Callable[[str, str], None]) -> None:
    """Arch §58: a destructive command asks first, and "no" is not a failure."""
    add_integration("ghost", "http://127.0.0.1:9/mcp")

    result = cli.run("remove", "ghost", stdin="n\n")

    assert result.returncode == EXIT_ABORTED
    assert (cli.config_dir / "manifests" / "ghost.yaml").exists(), "nothing may be deleted on a decline"


# ------------------------------------------------------------------- machine output


def test_json_output_is_parseable(cli: Cli, add_integration: Callable[[str, str], None]) -> None:
    """`--json` is the scripting contract, so it must not carry table decoration."""
    add_integration("ghost", "http://127.0.0.1:9/mcp")

    payload = cli.json("list")

    assert isinstance(payload, dict)
    assert any(entry["id"] == "ghost" for entry in payload["integrations"])


def test_json_output_on_failure_is_still_json(cli: Cli) -> None:
    """A script parsing stdout must not have to special-case the error path."""
    result = cli.run("--json", "show", "ghost")

    assert result.returncode == EXIT_FAILURE
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "integration_not_found"
    assert payload["error"]["message"]


# ------------------------------------------------------------------------- excludes


@pytest.mark.parametrize(
    "arguments",
    [
        ["--all", "--exclude", "a", "--exclude", "b"],
        ["--all", "--exclude", "a,b"],
    ],
    ids=["repeated", "comma-separated"],
)
def test_exclude_accepts_both_spellings(
    cli: Cli, add_integration: Callable[[str, str], None], arguments: list[str]
) -> None:
    """Arch §17 shows both forms, so both have to work — including together."""
    add_integration("a", "http://127.0.0.1:9/mcp")

    result = cli.run("update", *arguments, "--dry-run")

    assert result.returncode == EXIT_OK
    assert "a" in result.stdout


def test_a_dry_run_reports_without_changing_anything(cli: Cli, add_integration: Callable[[str, str], None]) -> None:
    add_integration("ghost", "http://127.0.0.1:9/mcp")
    lock = cli.config_dir / "integrations.lock.yaml"

    result = cli.run("update", "--all", "--dry-run")

    assert result.returncode == EXIT_OK
    assert not lock.exists(), "a dry run must not write resolved state"


# -------------------------------------------------------------------------- secrets


def test_there_is_no_way_to_pass_a_credential_as_an_argument(cli: Cli) -> None:
    """Arch §21: an argument lands in shell history and the process table.

    This is a test about an *absent* feature, which is the only way to keep one
    absent — a helpfully-added `--value` flag would otherwise pass every other
    test in the suite. It asserts on the parser rather than on the help text,
    because the help text explains why the flag does not exist and would match a
    naive substring search.
    """
    rejected = cli.run("secrets", "set", "BRAVE_API_KEY", "--value", "a-real-looking-key")
    assert rejected.returncode == EXIT_USAGE

    listed = cli.run("secrets", "set", "--help")
    assert listed.returncode == EXIT_OK
    for channel in ("--from-env", "--stdin"):
        assert channel in listed.stdout


def test_a_credential_read_from_stdin_is_never_echoed(cli: Cli) -> None:
    result = cli.run("secrets", "set", "BRAVE_API_KEY", "--stdin", stdin="a-real-looking-key\n")

    assert result.returncode == EXIT_OK
    assert "a-real-looking-key" not in result.stdout + result.stderr


def test_listing_credentials_shows_names_only(cli: Cli) -> None:
    cli.run("secrets", "set", "BRAVE_API_KEY", "--stdin", stdin="a-real-looking-key\n")

    result = cli.run("secrets", "list")

    assert result.returncode == EXIT_OK
    assert "BRAVE_API_KEY" in result.stdout
    assert "a-real-looking-key" not in result.stdout


# --------------------------------------------------------------------------- tokens


def test_issuing_a_token_prints_it_once(cli: Cli) -> None:
    payload = cli.json("token", "issue", "alice@example.com", "--scope", "tools:call")

    assert isinstance(payload, dict)
    assert payload["subject"] == "alice@example.com"
    assert payload["scopes"] == ["tools:call"]
    assert payload["token"].count(".") == 2, "a JWT has three segments"


def test_issuing_a_token_without_a_secret_fails_cleanly(cli: Cli) -> None:
    """Not a traceback: an unsigned token would be a credential anyone could forge."""
    cli._env["MCP_HUB_AUTH_SECRET"] = ""

    result = cli.run("token", "issue", "alice@example.com")

    assert result.returncode == EXIT_FAILURE
    assert "MCP_HUB_AUTH_SECRET" in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
