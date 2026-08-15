"""The `mcp-hub` command line (arch §14, §35, §36, §72).

Arch §72 is the rule this module exists to obey:

    Every command must operate against the same underlying integration-management
    service used by the REST API. Do not create separate business logic for CLI
    and API.

So nothing here decides what "update" means. Each command builds a `HubRuntime`
— the same composition root the ASGI application builds — and calls
`LifecycleService` or `UpdateManager`, exactly as the REST handlers do. What
lives here is argument parsing, confirmation, and rendering; if a command needed
a rule of its own, that would be a signal the rule belongs in the service layer.

Commands run **in this process**, against the configuration and runtime
directories the settings point at. To manage a containerised hub, run the CLI
inside the container (`docker compose exec mcp-hub mcp-hub update jira`) so it
sees the same `config/` and `runtime/` volumes. That keeps one deployment with
one source of truth rather than a second, half-authorised control plane.

Concurrency is real and is handled: lifecycle operations take the same
integration locks the server takes, so a CLI update and an API update cannot
overlap — *provided* Redis is configured, since without it locks are
process-local. Mutating commands say so out loud when they are not coordinated.

Every command accepts `--json`. The document it prints is the service layer's
own payload, identical to the REST response for the same operation, so scripts
never parse a table.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.cli.console import (
    Output,
    audit_table,
    disclosure_panel,
    doctor_report,
    health_table,
    integrations_table,
    outcomes_table,
    registry_table,
    rollback_points_table,
    status_report,
    tools_table,
)
from app.core.clock import utcnow
from app.core.context import Principal, RequestContext, bind_request
from app.core.domain import AuditAction, HealthStatus
from app.core.errors import HubError
from app.core.ids import new_request_id
from app.integrations.base import VersionRef
from app.server.runtime import HubRuntime

__all__ = ["app", "main"]

EXIT_OK = 0
EXIT_FAILURE = 1
"""The operation ran but its result is a failure — an unhealthy integration, a
failed update, a doctor check that did not pass."""

EXIT_ABORTED = 130
"""Interrupted, or a confirmation the operator declined."""

_HELP = """\
Manage the MCP Hub: one MCP endpoint fronting many governed integrations.

Commands run against the configuration in --config-dir and the state in
--runtime-dir. Add --json to any command for machine-readable output.
"""

app = typer.Typer(
    name="mcp-hub",
    help=_HELP,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
registry_app = typer.Typer(
    name="registry", help="Search and install from the MCP Registry (arch §7).", no_args_is_help=True
)
secrets_app = typer.Typer(name="secrets", help="Store integration credentials (arch §21).", no_args_is_help=True)
token_app = typer.Typer(name="token", help="Mint hub access tokens (arch §20).", no_args_is_help=True)
app.add_typer(registry_app)
app.add_typer(secrets_app)
app.add_typer(token_app)


# --------------------------------------------------------------------------- state


@dataclass(slots=True)
class CliState:
    """Everything the root options decided, carried to each command."""

    out: Output = field(default_factory=Output)
    overrides: dict[str, Any] = field(default_factory=dict)
    verbose: int = 0

    def settings(self) -> Any:
        """Build `Settings` for this invocation.

        The CLI is quiet by default. Not to hide problems — every condition the
        hub logs at WARNING during a command (no Redis, an unreachable upstream,
        an update that dropped a tool) is something the command's own output
        states in the place an operator is already looking. Repeating them as log
        lines over the top of a table teaches people to skim warnings, which is
        the opposite of what warnings are for. `-v` restores the stream.
        """
        from app.config.settings import load_settings

        overrides = dict(self.overrides)
        if self.verbose >= 2:
            overrides["log_level"] = "DEBUG"
        elif self.verbose == 1:
            overrides["log_level"] = "INFO"
        elif "MCP_HUB_LOG_LEVEL" not in os.environ:
            overrides["log_level"] = "ERROR"
        if "MCP_HUB_LOG_JSON" not in os.environ:
            overrides["log_json"] = False
        try:
            return load_settings(**overrides)
        except (ValidationError, SettingsError) as exc:
            # A malformed `MCP_HUB_*` value is the most likely first-run failure,
            # and pydantic's own report arrives as a traceback naming an internal
            # settings source. Whoever hits this is editing `.env`, so the answer
            # they need is which variable and what shape it wants.
            self.out.fail("Configuration is invalid; the hub cannot start.")
            for line in str(exc).splitlines():
                self.out.note(f"  {line}")
            self.out.note("Check your MCP_HUB_* environment or .env against .env.example.")
            raise typer.Exit(EXIT_FAILURE) from None


def _state(ctx: typer.Context) -> CliState:
    """The state the root callback stashed on the Typer context."""
    assert isinstance(ctx.obj, CliState)
    return ctx.obj


def _operator() -> Principal:
    """Who the CLI acts as.

    A local shell already proves operator access to the host, so the CLI holds
    every scope. The subject still names the human, because arch §24 wants the
    audit trail to say who ran the command rather than "the CLI".
    """
    from app.auth.permissions import ALL_SCOPES

    user = "unknown"
    with suppress(KeyError, OSError, ImportError):
        import getpass

        user = getpass.getuser()
    return Principal(
        subject=f"cli:{user}",
        display_name=f"{user} (cli)",
        scopes=frozenset(ALL_SCOPES),
        is_service_account=True,
    )


@asynccontextmanager
async def _session(state: CliState, *, discover: bool) -> AsyncIterator[HubRuntime]:
    """Build, start, and tear down a runtime for one command.

    Args:
        discover: Contact every upstream and populate the tool registry. Only
            the commands that display tools or live health pay for it; a
            configuration change does its own targeted discovery.
    """
    runtime = await HubRuntime.create(state.settings())
    context = RequestContext(request_id=new_request_id(), principal=_operator(), source="cli")
    with bind_request(context):
        try:
            await runtime.start(discover=discover)
            yield runtime
        finally:
            await runtime.stop()


Operation = Callable[[HubRuntime], Awaitable[int]]
"""A command body: given a started runtime, do the work and return an exit code."""


def _execute(state: CliState, operation: Operation, *, discover: bool = False) -> None:
    """Run one command body, translating failures into exit codes.

    Every deliberate hub failure is a `HubError` carrying a stable code and a
    caller-safe message (see `app/core/errors.py`), so this prints the message
    and the code and exits 1. Anything else is a bug and keeps its traceback,
    because swallowing it would make the hub harder to fix.
    """

    async def run() -> int:
        async with _session(state, discover=discover) as runtime:
            return await operation(runtime)

    try:
        code = asyncio.run(run())
    except HubError as exc:
        state.out.fail(exc.message)
        state.out.error_detail(exc.code, exc.details)
        if state.out.json_mode:
            state.out.json({"error": exc.to_payload()})
        raise typer.Exit(EXIT_FAILURE) from None
    except KeyboardInterrupt:
        state.out.fail("Interrupted.")
        raise typer.Exit(EXIT_ABORTED) from None
    if code:
        raise typer.Exit(code)


def _warn_if_uncoordinated(state: CliState, runtime: HubRuntime) -> None:
    """Say so when lifecycle locks are not shared across processes (arch §28, §30)."""
    if runtime.redis is None:
        state.out.warn(
            "Redis is not configured, so lifecycle locks are local to this process. Avoid running "
            "this while a hub server is serving traffic against the same config."
        )


# --------------------------------------------------------------------------- helpers


def _split_names(values: Sequence[str] | None) -> tuple[str, ...]:
    """Flatten repeated and comma-separated name lists.

    `--exclude jira --exclude figma` and `--exclude jira,figma` mean the same
    thing (arch §14).
    """
    names: list[str] = []
    for value in values or ():
        names.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(names))


def _targets_and_exclusions(
    names: Sequence[str] | None,
    *,
    all_integrations: bool,
    exclude: Sequence[str] | None,
    command: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve what to act on and what to leave alone.

    Arch §72 requires both spellings of a multi-value exclusion to work:

        mcp-hub update --all --exclude jira,figma
        mcp-hub update --all --exclude jira figma

    The second puts `figma` in the positional list, so when `--all` is set,
    trailing positionals are exclusions rather than targets — there is nothing
    else they could mean, since `--all` already names everything.

    Raises:
        typer.BadParameter: Names were given with `--all` but no `--exclude`,
            which is ambiguous rather than merely redundant.
    """
    excluded = list(_split_names(exclude))
    targets = list(names or ())

    if all_integrations and targets:
        if not excluded:
            raise typer.BadParameter(
                f"Naming integrations together with --all is ambiguous. "
                f"Use `mcp-hub {command} {' '.join(targets)}` to act on those, "
                f"or `mcp-hub {command} --all` to act on every enabled integration.",
                param_hint="--all",
            )
        excluded.extend(targets)
        targets = []

    if not targets and not all_integrations:
        raise typer.BadParameter(
            f"Name at least one integration, or pass --all. "
            f"Example: `mcp-hub {command} jira` or `mcp-hub {command} --all`.",
        )
    return tuple(targets), tuple(dict.fromkeys(excluded))


_DURATION = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _parse_since(value: str | None) -> datetime | None:
    """Accept `30m`, `2h`, `7d`, or an ISO timestamp.

    Raises:
        typer.BadParameter: The value is neither.
    """
    if not value:
        return None
    match = _DURATION.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), _DURATION_UNITS[match.group(2)]
        return utcnow() - timedelta(**{unit: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{value!r} is neither a duration like 30m/2h/7d nor an ISO timestamp.") from None
    return parsed


# --------------------------------------------------------------------------- root


def _version_callback(value: bool) -> None:
    if value:
        from app import __version__

        typer.echo(f"mcp-hub {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def root(
    ctx: typer.Context,
    config_dir: Annotated[
        Path | None,
        typer.Option("--config-dir", envvar="MCP_HUB_CONFIG_DIR", help="Directory holding the YAML contract."),
    ] = None,
    runtime_dir: Annotated[
        Path | None,
        typer.Option("--runtime-dir", envvar="MCP_HUB_RUNTIME_DIR", help="Directory holding mutable state."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the machine-readable payload instead of a table.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress progress notes.")] = False,
    verbose: Annotated[
        int,
        typer.Option("--verbose", "-v", count=True, help="Show the hub's logs: -v for info, -vv for debug."),
    ] = 0,
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Print the version and exit."),
    ] = False,
) -> None:
    """Manage the MCP Hub."""
    overrides: dict[str, Any] = {}
    if config_dir is not None:
        overrides["config_dir"] = config_dir
    if runtime_dir is not None:
        overrides["runtime_dir"] = runtime_dir
    ctx.obj = CliState(
        out=Output(json_mode=json_output, quiet=quiet),
        overrides=overrides,
        verbose=verbose,
    )


# --------------------------------------------------------------------------- views


@app.command("list")
def list_command(
    ctx: typer.Context,
    enabled_only: Annotated[bool, typer.Option("--enabled-only", help="Only integrations switched on.")] = False,
    probe: Annotated[
        bool,
        typer.Option("--probe/--no-probe", help="Contact upstreams for live health and tool counts."),
    ] = True,
) -> None:
    """List every configured integration and its state (arch §14)."""
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        rows = [row.to_payload() for row in runtime.lifecycle.list_integrations(enabled_only=enabled_only)]
        state.out.emit(
            {"integrations": rows, "count": len(rows), "probed": probe},
            lambda: integrations_table(rows, probed=probe),
        )
        return EXIT_OK

    _execute(state, operation, discover=probe)


@app.command("status")
def status_command(ctx: typer.Context) -> None:
    """Show what the hub is doing right now."""
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        payload = runtime.status_payload()
        state.out.emit(payload, lambda: status_report(payload))
        return EXIT_OK

    _execute(state, operation, discover=True)


@app.command("show")
def show_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Integration id.")],
) -> None:
    """Show one integration in full: source, lock, health, tools, policy."""
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        await runtime.manager.check_health(name)
        payload = runtime.lifecycle.describe(name)
        state.out.emit(payload, lambda: _render_description(payload))
        return EXIT_OK

    _execute(state, operation, discover=True)


def _render_description(payload: dict[str, Any]) -> Any:
    """Render `mcp-hub show`."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    facts = Table(box=None, show_header=False, pad_edge=False)
    facts.add_column(style="dim")
    facts.add_column()
    lock = payload.get("lock") or {}
    rows = [
        ("id", payload["id"]),
        ("name", payload["name"]),
        ("description", payload.get("description") or "-"),
        ("namespace", payload["namespace"]),
        ("source", f"{payload['source'].get('type', '?')} — {payload['origin']}"),
        ("trust", payload["trust"]),
        ("enabled", "yes" if payload["enabled"] else "no"),
        ("installed", "yes" if payload["installed"] else "no"),
        ("version", str(lock.get("version_id") or "-")),
        ("update policy", payload["update_policy"]),
        ("capabilities", ", ".join(payload.get("capabilities", ())) or "-"),
        ("health", f"{payload['health']['status']} {payload['health'].get('detail') or ''}".strip()),
    ]
    for label, value in rows:
        facts.add_row(label, str(value))

    parts: list[Any] = [facts]
    if payload.get("tools"):
        parts.append(Text("\nTools", style="bold"))
        parts.append(tools_table(payload["tools"]))
    if payload.get("rollback_points"):
        parts.append(Text("\nRollback points", style="bold"))
        parts.append(rollback_points_table(payload["rollback_points"]))
    return Group(*parts)


@app.command("health")
def health_command(
    ctx: typer.Context,
    names: Annotated[list[str] | None, typer.Argument(help="Integrations to probe. Default: all.")] = None,
) -> None:
    """Probe integrations and report their status (arch §25).

    Exits non-zero when an enabled integration is UNAVAILABLE, so this works as a
    deployment gate. Degraded and auth-required states are reported but do not
    fail the command: the hub is still serving (arch §45).
    """
    state = _state(ctx)
    targets = _split_names(names)

    async def operation(runtime: HubRuntime) -> int:
        statuses = await runtime.lifecycle.health(integrations=targets or None)
        healthy = sum(1 for report in statuses.values() if report["status"] == HealthStatus.HEALTHY.value)
        state.out.emit(
            {"integrations": statuses, "healthy": healthy, "total": len(statuses)},
            lambda: health_table(statuses),
        )
        unavailable = [
            name for name, report in statuses.items() if report["status"] == HealthStatus.UNAVAILABLE.value
        ]
        if unavailable:
            state.out.fail(f"Unavailable: {', '.join(unavailable)}")
            return EXIT_FAILURE
        return EXIT_OK

    _execute(state, operation)


@app.command("tools")
def tools_command(
    ctx: typer.Context,
    integration: Annotated[str | None, typer.Argument(help="Restrict to one integration.")] = None,
    search: Annotated[
        str | None, typer.Option("--search", "-s", help="Substring match on name or description.")
    ] = None,
) -> None:
    """List the tools the hub exposes (arch §51).

    Names are shown qualified, exactly as an agent sees them (arch §11).
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        if integration:
            runtime.manager.get(integration)  # raises IntegrationNotFound with the known ids
            descriptors = list(runtime.registry.for_integration(integration))
        else:
            descriptors = runtime.registry.all()
        rows = [item.summary() for item in descriptors]
        if search:
            needle = search.lower()
            rows = [
                row
                for row in rows
                if needle in row["qualified_name"].lower() or needle in (row["description"] or "").lower()
            ]
        mode = runtime.gateway.exposure_mode.value
        state.out.emit(
            {"tools": rows, "count": len(rows), "exposure_mode": mode},
            lambda: tools_table(rows, exposure_mode=mode),
        )
        return EXIT_OK

    _execute(state, operation, discover=True)


@app.command("logs")
def logs_command(
    ctx: typer.Context,
    integration: Annotated[str | None, typer.Argument(help="Restrict to one integration.")] = None,
    lines: Annotated[int, typer.Option("--lines", "-n", min=1, max=1000, help="How many records.")] = 50,
    action: Annotated[str | None, typer.Option("--action", help="Filter by audit action.")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Filter by principal subject.")] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Only records newer than this: 30m, 2h, 7d, or an ISO timestamp.")
    ] = None,
    process_log: Annotated[
        bool,
        typer.Option("--process-log", help="Tail the integration's own stderr log instead of the audit trail."),
    ] = False,
) -> None:
    """Read the audit trail, or an integration's process log (arch §24).

    The audit trail is the hub's record of what was attempted and decided; the
    process log is what a locally-run server printed. They answer different
    questions, so they are different flags rather than one merged stream.
    """
    state = _state(ctx)
    cutoff = _parse_since(since)
    parsed_action: AuditAction | None = None
    if action:
        try:
            parsed_action = AuditAction(action)
        except ValueError:
            known = ", ".join(sorted(item.value for item in AuditAction))
            raise typer.BadParameter(f"Unknown action {action!r}. Known actions: {known}.") from None

    async def operation(runtime: HubRuntime) -> int:
        if process_log:
            return _tail_process_log(state, runtime, integration, lines)

        records = await runtime.audit.query(
            limit=lines,
            integration=integration,
            action=parsed_action,
            user_id=user,
            since=cutoff,
        )
        rows = [
            {
                "timestamp": record.timestamp.isoformat(),
                "action": record.action,
                "status": record.status,
                "user_id": record.user_id,
                "integration": record.integration,
                "tool": record.tool,
                "risk_level": record.risk_level,
                "decision": record.decision,
                "duration_ms": record.duration_ms,
                "error_code": record.error_code,
                "message": record.message,
            }
            for record in records
        ]
        state.out.emit({"records": rows, "count": len(rows)}, lambda: audit_table(rows))
        return EXIT_OK

    _execute(state, operation)


def _tail_process_log(state: CliState, runtime: HubRuntime, integration: str | None, lines: int) -> int:
    """Show the last `lines` of a locally-run integration's log file."""
    if not integration:
        raise typer.BadParameter("--process-log needs an integration name.")
    path = runtime.settings.logs_dir / f"{integration}.log"
    if not path.exists():
        state.out.warn(
            f"No process log at {path}. Remote integrations have none, and a local one writes "
            "its first line when it starts."
        )
        state.out.emit({"integration": integration, "path": str(path), "lines": []}, lambda: "")
        return EXIT_OK

    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = content[-lines:]
    state.out.emit(
        {"integration": integration, "path": str(path), "lines": tail},
        lambda: "\n".join(tail),
    )
    return EXIT_OK


@app.command("doctor")
def doctor_command(ctx: typer.Context) -> None:
    """Check the environment, configuration, and every integration (arch §36).

    Exits non-zero if any check FAILs, so it doubles as a pre-flight gate.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        checks = [
            {"status": status, "subject": subject, "detail": detail}
            for status, subject, detail in await runtime.lifecycle.doctor()
        ]
        failed = [check for check in checks if check["status"] == "FAIL"]
        state.out.emit({"checks": checks, "ok": not failed}, lambda: doctor_report(checks))
        return EXIT_FAILURE if failed else EXIT_OK

    _execute(state, operation)


# --------------------------------------------------------------------------- lifecycle


@app.command("install")
def install_command(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Integrations to install.")],
    enable: Annotated[bool, typer.Option("--enable/--no-enable", help="Switch on after installing.")] = True,
    version: Annotated[
        str | None, typer.Option("--version", help="Install this exact version instead of the latest.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the community-code confirmation.")] = False,
) -> None:
    """Install an integration (arch §14).

    Community-tier sources run third-party code on this host, so they need an
    explicit confirmation first (arch §7, §54). Official remote services need
    none — nothing is installed locally.
    """
    state = _state(ctx)
    targets = _split_names(names)
    if version and len(targets) > 1:
        raise typer.BadParameter("--version applies to a single integration.")

    async def operation(runtime: HubRuntime) -> int:
        _warn_if_uncoordinated(state, runtime)
        pinned = VersionRef(identifier=version, kind="version") if version else None
        results: list[dict[str, Any]] = []
        failures = 0

        for name in targets:
            integration = runtime.manager.get(name)
            if integration.trust.requires_explicit_trust and not state.out.confirm(
                f"{name} is a community integration ({integration.manifest.source.type}). "
                "Installing runs third-party code on this host. Continue?",
                assume_yes=yes,
            ):
                state.out.fail(f"{name}: not installed — confirmation declined.")
                failures += 1
                continue

            state.out.note(f"Installing {name}…")
            result = await runtime.lifecycle.install(name, enable=enable, version=pinned)
            results.append(result)
            state.out.success(
                f"{name} installed at {result['version']} — {result['tools']} tool(s), "
                f"{'enabled' if result['enabled'] else 'left disabled'}."
            )

        state.out.emit({"installed": results, "failed": failures}, lambda: _installed_summary(results))
        return EXIT_FAILURE if failures else EXIT_OK

    _execute(state, operation)


def _installed_summary(results: Sequence[dict[str, Any]]) -> Any:
    from rich.text import Text

    if not results:
        return Text("Nothing was installed.", style="dim")
    lines = [
        f"{item['integration']}: {item['version']} — {item['tools']} tool(s), status {item['status']}"
        for item in results
    ]
    return Text("\n".join(lines))


@app.command("enable")
def enable_command(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Integrations to switch on.")],
) -> None:
    """Switch an integration on and discover its tools (arch §14)."""
    state = _state(ctx)
    targets = _split_names(names)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        results = []
        for name in targets:
            result = await runtime.lifecycle.enable(name)
            results.append(result)
            state.out.success(f"{name} enabled — {result['tools']} tool(s), status {result['status']}.")
        state.out.emit(
            {"enabled": results},
            lambda: Text("\n".join(f"{item['integration']}: {item['tools']} tool(s)" for item in results)),
        )
        return EXIT_OK

    _execute(state, operation)


@app.command("disable")
def disable_command(
    ctx: typer.Context,
    names: Annotated[list[str], typer.Argument(help="Integrations to switch off.")],
) -> None:
    """Switch an integration off (arch §14).

    Its tools stop being routed and its sessions close. Nothing is deleted, so
    re-enabling is immediate and needs no reinstall.
    """
    state = _state(ctx)
    targets = _split_names(names)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        results = []
        for name in targets:
            result = await runtime.lifecycle.disable(name)
            results.append(result)
            state.out.success(
                f"{name} disabled — {result['tools_withdrawn']} tool(s) withdrawn, "
                f"{result['sessions_closed']} session(s) closed."
            )
        state.out.emit(
            {"disabled": results},
            lambda: Text("\n".join(f"{item['integration']}: withdrawn" for item in results)),
        )
        return EXIT_OK

    _execute(state, operation)


@app.command("update")
def update_command(
    ctx: typer.Context,
    names: Annotated[list[str] | None, typer.Argument(help="Integrations to update.")] = None,
    all_integrations: Annotated[bool, typer.Option("--all", help="Update every enabled integration.")] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Leave these alone. Repeatable, or comma-separated (arch §17)."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and change nothing (arch §59).")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Execute the plan without asking.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Update even when already at the resolved version.")] = False,
    rollback_on_failure: Annotated[
        bool | None,
        typer.Option(
            "--rollback-on-failure/--no-rollback-on-failure",
            help="Restore the previous version if an update fails. Default: on (arch §59).",
        ),
    ] = None,
    parallel: Annotated[
        bool, typer.Option("--parallel", help="Update integrations concurrently (arch §30). Off by default.")
    ] = False,
) -> None:
    """Update integrations through the staged pipeline (arch §15, §16, §17).

    Nothing is updated in place. Each version stages into its own directory,
    proves it can start and list tools while the current one keeps serving, and
    only then is promoted by an atomic rename. A failure after promotion rolls
    back automatically unless told not to.

    [bold]Examples[/bold]

        mcp-hub update jira
        mcp-hub update jira figma github
        mcp-hub update --all
        mcp-hub update --all --exclude jira
        mcp-hub update --all --exclude jira,figma
        mcp-hub update --all --exclude jira figma
        mcp-hub update --all --dry-run
    """
    state = _state(ctx)
    targets, excluded = _targets_and_exclusions(
        names, all_integrations=all_integrations, exclude=exclude, command="update"
    )

    async def operation(runtime: HubRuntime) -> int:
        state.out.note("Resolving versions…")
        plan = await runtime.updater.plan(names=targets, all_integrations=all_integrations, exclude=excluded)

        if dry_run:
            state.out.emit(
                {"dry_run": True, "plan": plan.to_payload(), "rendered": plan.render()},
                lambda: plan.render(),
            )
            return EXIT_OK

        if plan.is_empty:
            state.out.emit(
                {"plan": plan.to_payload(), "outcomes": [], "succeeded": 0, "failed": 0},
                lambda: plan.render(),
            )
            state.out.success("Everything is already up to date.")
            return EXIT_OK

        state.out.print(plan.render())
        if not state.out.confirm(f"Apply {len(plan.actionable)} change(s)?", assume_yes=yes, default=True):
            state.out.fail("Aborted; nothing was changed.")
            return EXIT_ABORTED

        _warn_if_uncoordinated(state, runtime)
        # The approved plan is handed back rather than recomputed, so what runs
        # is exactly what was shown (arch §16).
        _, outcomes = await runtime.updater.update(
            names=targets,
            all_integrations=all_integrations,
            exclude=excluded,
            force=force,
            rollback_on_failure=rollback_on_failure,
            parallel=parallel,
            plan=plan,
        )
        payloads = [outcome.summary() for outcome in outcomes]
        failed = [item for item in payloads if not item["succeeded"]]
        state.out.emit(
            {
                "plan": plan.to_payload(),
                "outcomes": payloads,
                "succeeded": len(payloads) - len(failed),
                "failed": len(failed),
            },
            lambda: outcomes_table(payloads),
        )
        return EXIT_FAILURE if failed else EXIT_OK

    _execute(state, operation, discover=True)


@app.command("remove")
def remove_command(
    ctx: typer.Context,
    names: Annotated[list[str] | None, typer.Argument(help="Integrations to remove.")] = None,
    all_integrations: Annotated[bool, typer.Option("--all", help="Remove every configured integration.")] = False,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", help="Keep these. Repeatable, or comma-separated.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation (arch §58).")] = False,
    keep_secrets: Annotated[
        bool, typer.Option("--keep-secrets", help="Leave this integration's own credentials in place.")
    ] = False,
    purge_backups: Annotated[
        bool, typer.Option("--purge-backups", help="Also delete its rollback points, making this irreversible.")
    ] = False,
) -> None:
    """Remove integrations entirely (arch §18).

    Routing stops first, then sessions close, then a configuration backup is
    taken, then artifacts and this integration's own credentials are deleted.
    Credentials shared with another integration are never touched.

    Rollback points are kept unless `--purge-backups` is passed, so a removal
    stays reversible for a while.
    """
    state = _state(ctx)
    targets, excluded = _targets_and_exclusions(
        names, all_integrations=all_integrations, exclude=exclude, command="remove"
    )

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        chosen = (
            [item.id for item in runtime.manager.all() if item.id not in excluded]
            if all_integrations
            else list(targets)
        )
        for name in chosen:
            runtime.manager.get(name)  # fail before removing anything if a name is wrong

        if not chosen:
            state.out.warn("Nothing matched; nothing was removed.")
            state.out.emit({"removed": []}, lambda: Text("Nothing to remove.", style="dim"))
            return EXIT_OK

        state.out.print(Text(f"About to remove: {', '.join(chosen)}", style="bold yellow"))
        if not keep_secrets:
            state.out.note("Credentials owned solely by these integrations will be deleted.")
        if purge_backups:
            state.out.warn("--purge-backups is set: this cannot be rolled back afterwards.")
        if not state.out.confirm(f"Remove {len(chosen)} integration(s)?", assume_yes=yes):
            state.out.fail("Aborted; nothing was removed.")
            return EXIT_ABORTED

        _warn_if_uncoordinated(state, runtime)
        results = []
        for name in chosen:
            result = await runtime.lifecycle.remove(name, purge_secrets=not keep_secrets, purge_backups=purge_backups)
            results.append(result)
            state.out.success(
                f"{name} removed — {result['tools_withdrawn']} tool(s) withdrawn, "
                f"{result['secrets_removed']} credential(s) deleted."
            )
        state.out.emit(
            {"removed": results},
            lambda: Text("\n".join(f"{item['integration']}: removed" for item in results)),
        )
        return EXIT_OK

    _execute(state, operation, discover=True)


@app.command("rollback")
def rollback_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Integration to roll back.")],
    version: Annotated[
        str | None, typer.Option("--version", help="Restore this version. Default: the most recent rollback point.")
    ] = None,
    list_points: Annotated[bool, typer.Option("--list", help="Show the available rollback points and exit.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Restore a previous version (arch §19).

    The previous version is still on disk — updates never delete what they
    replace — so this is an atomic rename back, not a re-download.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        runtime.manager.get(name)
        points = runtime.lifecycle.rollback_points(name)

        if list_points:
            state.out.emit(
                {"integration": name, "rollback_points": points},
                lambda: rollback_points_table(points),
            )
            return EXIT_OK

        target = version or (points[0]["version"] if points else None)
        if not state.out.confirm(
            f"Roll {name} back to {target or 'its previous version'}?", assume_yes=yes, default=True
        ):
            state.out.fail("Aborted; nothing was changed.")
            return EXIT_ABORTED

        _warn_if_uncoordinated(state, runtime)
        result = await runtime.lifecycle.rollback(name, version=version)
        state.out.emit(
            result,
            lambda: Text(
                f"{result['integration']}: {result['from'] or 'unknown'} → {result['to']} "
                f"({result['health']}, {result['tools']} tool(s))"
            ),
        )
        if not HealthStatus(result["health"]).can_serve:
            state.out.fail(f"{name} is {result['health']} after the rollback.")
            return EXIT_FAILURE
        state.out.success(f"{name} rolled back to {result['to']}.")
        return EXIT_OK

    _execute(state, operation, discover=True)


# --------------------------------------------------------------------------- state sync


@app.command("refresh")
def refresh_command(
    ctx: typer.Context,
    names: Annotated[list[str] | None, typer.Argument(help="Integrations to refresh. Default: all.")] = None,
) -> None:
    """Re-read tool definitions from upstreams (arch §12).

    Tools are discovered, never hard-coded, so this is how an upstream's new
    tool becomes visible without touching the hub.
    """
    state = _state(ctx)
    targets = _split_names(names)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        results = await runtime.lifecycle.refresh(integrations=targets or None)
        total = sum(item["tools"] for item in results.values())
        state.out.emit(
            {"integrations": results, "tools": total},
            lambda: Text(
                "\n".join(f"{name}: {item['tools']} tool(s) — {item['status']}" for name, item in results.items())
                + f"\n\n{total} tool(s) registered."
            ),
        )
        return EXIT_OK

    _execute(state, operation)


@app.command("reconcile")
def reconcile_command(ctx: typer.Context) -> None:
    """Drive actual state toward the configuration on disk (arch §63).

    Reads `integrations.yaml` fresh, so an operator who hand-edited it gets the
    change applied without restarting the hub. Each integration reconciles
    independently: one failure is reported and the rest proceed.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        report = await runtime.lifecycle.reconcile()
        state.out.emit(report.to_payload(), lambda: report.render())
        return EXIT_FAILURE if report.failed else EXIT_OK

    _execute(state, operation, discover=True)


@app.command("sync")
def sync_command(ctx: typer.Context) -> None:
    """Reload configuration, reconcile, and rediscover every tool (arch §14).

    The catch-all after editing configuration by hand: it is `reconcile`
    followed by a full `refresh`, so both the catalog and the tool registry end
    up matching what is on disk and what the upstreams actually expose.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        from rich.console import Group
        from rich.text import Text

        catalog = await runtime.reload_configuration()
        state.out.note(f"Configuration reloaded — {len(catalog)} integration(s).")
        report = await runtime.lifecycle.reconcile()
        tools = await runtime.lifecycle.refresh()
        total = sum(item["tools"] for item in tools.values())
        state.out.emit(
            {
                "integrations": len(catalog),
                "reconcile": report.to_payload(),
                "tools": tools,
                "tool_count": total,
            },
            lambda: Group(
                Text(report.render()),
                Text(f"\n{total} tool(s) registered across {len(tools)} integration(s)."),
            ),
        )
        return EXIT_FAILURE if report.failed else EXIT_OK

    _execute(state, operation)


# --------------------------------------------------------------------------- registry


@registry_app.command("search")
def registry_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search terms.")] = "",
    limit: Annotated[int, typer.Option("--limit", min=1, max=100, help="Maximum results.")] = 25,
) -> None:
    """Search the official MCP Registry (arch §7).

    Reading the registry installs nothing. `registry inspect` shows what a
    server would run; `registry install` is the step that acts.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        results = await runtime.registry_client.search_servers(query, limit=limit)
        rows = [
            {
                "name": server.name,
                "description": server.description,
                "version": server.version,
                "repository": server.repository_url,
                "deprecated": server.is_deprecated,
                "installable": server.preferred_installation() is not None,
            }
            for server in results.servers
        ]
        state.out.emit(
            {"query": query, "servers": rows, "count": len(rows), "next_cursor": results.next_cursor},
            lambda: registry_table(rows),
        )
        return EXIT_OK

    _execute(state, operation)


@registry_app.command("inspect")
def registry_inspect(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Canonical registry name.")],
) -> None:
    """Show the pre-install disclosure for a registry entry (arch §54).

    Repository, owner, version, licence, runtime, the network and filesystem it
    wants, and every credential it asks for — without installing anything.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        disclosure = await runtime.registry_client.validate_server(name)
        state.out.emit(
            {**disclosure.to_payload(), "rendered": disclosure.render()},
            lambda: disclosure_panel(disclosure.render(), requires_confirmation=disclosure.requires_confirmation),
        )
        return EXIT_OK

    _execute(state, operation)


@registry_app.command("versions")
def registry_versions(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Canonical registry name.")],
) -> None:
    """List a registry entry's published versions (arch §7)."""
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        versions = await runtime.registry_client.get_versions(name)
        state.out.emit(
            {"server": name, "versions": versions},
            lambda: Text("\n".join(versions) if versions else "No published versions."),
        )
        return EXIT_OK

    _execute(state, operation)


@registry_app.command("install")
def registry_install(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Canonical registry name.")],
    integration_id: Annotated[
        str | None, typer.Option("--id", help="Install under this hub id. Derived from the name otherwise.")
    ] = None,
    enable: Annotated[bool, typer.Option("--enable/--no-enable", help="Switch on after installing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept the disclosure without prompting.")] = False,
) -> None:
    """Install a server from the registry (arch §56).

    Follows arch §56's order exactly: resolve the entry, show the disclosure,
    take an explicit decision, write the manifest, then install. A community
    server is never installed without a confirmation.
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        disclosure = await runtime.registry_client.validate_server(name, integration_id=integration_id)
        state.out.print(disclosure_panel(disclosure.render(), requires_confirmation=disclosure.requires_confirmation))

        if disclosure.requires_confirmation and not state.out.confirm(
            f"Install {disclosure.server_name} as {disclosure.integration_id}? "
            "This runs third-party code on this host.",
            assume_yes=yes,
        ):
            state.out.fail("Aborted; nothing was installed.")
            return EXIT_ABORTED

        assert disclosure.manifest is not None
        path = runtime.store.write_manifest(disclosure.manifest)
        state.out.note(f"Manifest written to {path}.")
        await runtime.reload_configuration()

        _warn_if_uncoordinated(state, runtime)
        result = await runtime.lifecycle.install(disclosure.integration_id, enable=enable)
        state.out.success(
            f"{disclosure.integration_id} installed at {result['version']} — {result['tools']} tool(s)."
        )
        state.out.emit(
            {"disclosure": disclosure.to_payload(), "manifest": str(path), "install": result},
            lambda: _installed_summary([result]),
        )
        return EXIT_OK

    _execute(state, operation)


# --------------------------------------------------------------------------- secrets


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name, as the manifest references it.")],
    principal: Annotated[
        str | None, typer.Option("--principal", help="Store as this user's own credential (arch §20).")
    ] = None,
    integration: Annotated[
        str | None,
        typer.Option("--integration", help="Record sole ownership so removal can clean it up (arch §18)."),
    ] = None,
    from_env: Annotated[
        str | None, typer.Option("--from-env", help="Read the value from this environment variable.")
    ] = None,
    from_stdin: Annotated[bool, typer.Option("--stdin", help="Read the value from standard input.")] = False,
) -> None:
    """Store a credential (arch §21).

    There is deliberately no `--value` flag: a credential passed as an argument
    lands in shell history and in the process table, where anyone on the host
    can read it. Use `--from-env`, `--stdin`, or the hidden prompt.
    """
    state = _state(ctx)

    if from_env:
        raw = os.environ.get(from_env)
        if raw is None:
            raise typer.BadParameter(f"Environment variable {from_env} is not set.")
    elif from_stdin:
        raw = sys.stdin.read().strip()
    elif sys.stdin.isatty():
        raw = typer.prompt(f"Value for {name}", hide_input=True, confirmation_prompt=True)
    else:
        raise typer.BadParameter("No terminal to prompt on. Use --from-env or --stdin.")
    if not raw:
        raise typer.BadParameter("Refusing to store an empty credential.")

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        from app.core.redaction import Secret

        provider = await runtime.secrets.store(name, Secret(raw), principal=principal, integration=integration)
        state.out.success(f"{name} stored in the {provider} provider.")
        state.out.emit(
            {"stored": name, "provider": provider, "per_user": bool(principal)},
            lambda: Text(f"{name} → {provider}"),
        )
        return EXIT_OK

    _execute(state, operation)


@secrets_app.command("list")
def secrets_list(
    ctx: typer.Context,
    principal: Annotated[
        str | None, typer.Option("--principal", help="Also list this user's own credentials.")
    ] = None,
) -> None:
    """List which credentials exist, by provider.

    Names only. The hub has no path that returns a credential value (arch §21).
    """
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        providers = await runtime.secrets.describe(principal=principal)
        lines = [
            f"{provider}: {', '.join(names) if names else '(none)'}" for provider, names in sorted(providers.items())
        ]
        state.out.emit({"providers": providers}, lambda: Text("\n".join(lines) or "No providers configured."))
        return EXIT_OK

    _execute(state, operation)


@secrets_app.command("delete")
def secrets_delete(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name.")],
    principal: Annotated[str | None, typer.Option("--principal", help="Delete this user's own credential.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Delete a credential (arch §21)."""
    state = _state(ctx)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        if not state.out.confirm(f"Delete credential {name}?", assume_yes=yes):
            state.out.fail("Aborted; nothing was deleted.")
            return EXIT_ABORTED
        removed = await runtime.secrets.delete(name, principal=principal)
        state.out.emit(
            {"deleted": name, "removed": removed},
            lambda: Text(f"{name}: {'deleted' if removed else 'not found'}"),
        )
        return EXIT_OK if removed else EXIT_FAILURE

    _execute(state, operation)


# --------------------------------------------------------------------------- tokens


@token_app.command("issue")
def token_issue(
    ctx: typer.Context,
    subject: Annotated[str, typer.Argument(help="Stable user identity the token represents.")],
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="Grant this scope. Repeatable. Default: an agent's scopes."),
    ] = None,
    ttl: Annotated[int, typer.Option("--ttl", min=60, max=86400, help="Lifetime in seconds.")] = 3600,
    name: Annotated[str | None, typer.Option("--name", help="Display name, for logs only.")] = None,
) -> None:
    """Mint a bearer token for an MCP client or a script (arch §20).

    The subject is what per-user credentials key off, so it must be the caller's
    real identity — not a label. The token is printed once and never stored.
    """
    state = _state(ctx)
    scopes = _split_names(scope)

    async def operation(runtime: HubRuntime) -> int:
        from rich.text import Text

        from app.auth.permissions import AGENT_SCOPES

        issued = runtime.token_issuer().issue(
            subject,
            scopes=scopes or AGENT_SCOPES,
            display_name=name,
            ttl_seconds=ttl,
        )
        token = issued.token.reveal()
        expires = datetime.fromtimestamp(issued.expires_at, tz=UTC).isoformat()
        state.out.emit(
            {"token": token, "subject": issued.subject, "scopes": list(issued.scopes), "expires_at": expires},
            lambda: Text(token),
        )
        state.out.note(f"Expires {expires}. Send it as `Authorization: Bearer <token>`.")
        return EXIT_OK

    _execute(state, operation)


# --------------------------------------------------------------------------- serve


@app.command("serve")
def serve_command(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port.")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Restart on code changes. Development only.")] = False,
) -> None:
    """Run the hub: the MCP endpoint and the management API (arch §10, §26)."""
    state = _state(ctx)
    settings = state.settings()

    import uvicorn

    state.out.note(f"MCP endpoint http://{host or settings.host}:{port or settings.port}{settings.mcp_path}")
    uvicorn.run(
        "app.main:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_config=None,  # structlog owns logging (arch §44)
        access_log=False,
    )


def main() -> None:
    """Console-script entry point (`mcp-hub`)."""
    app()


if __name__ == "__main__":
    main()
