"""Terminal rendering for the `mcp-hub` CLI (arch §14, §35, §36).

Two output modes, decided once and honoured by every command: tables for a
person at a terminal, and `--json` for scripts and CI. The JSON is not a second
rendering of the data — it is the service layer's own `to_payload()` output,
byte-identical to what the REST API returns for the same operation. So a script
that parses `mcp-hub list --json` sees exactly the fields `GET /api/integrations`
would have given it, and nothing has to be kept in sync by hand.

Data goes to stdout, diagnostics go to stderr. `mcp-hub list --json | jq` works
even while the hub is warning about a degraded upstream, and a redirected
stdout never picks up a progress note.

Confirmation fails closed. A destructive command with no TTY and no `--yes`
stops rather than guessing, because the one place a prompt matters is the one
where nobody is watching.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text

__all__ = [
    "Output",
    "audit_table",
    "disclosure_panel",
    "doctor_report",
    "health_table",
    "integrations_table",
    "outcomes_table",
    "registry_table",
    "rollback_points_table",
    "status_report",
    "tools_table",
]

_HEALTH_STYLES: Final[dict[str, str]] = {
    "HEALTHY": "bold green",
    "DEGRADED": "yellow",
    "UNAVAILABLE": "bold red",
    "DISABLED": "dim",
    "UPDATE_REQUIRED": "cyan",
    "AUTH_REQUIRED": "yellow",
}

_RISK_STYLES: Final[dict[str, str]] = {
    "READ": "green",
    "WRITE": "yellow",
    "DESTRUCTIVE": "bold red",
    "ADMIN": "bold magenta",
}

_CHECK_STYLES: Final[dict[str, str]] = {"OK": "green", "WARN": "yellow", "FAIL": "bold red"}

_TRUST_STYLES: Final[dict[str, str]] = {
    "remote_official": "green",
    "local_official": "cyan",
    "builtin": "blue",
    "community": "yellow",
}


def _console(*, stderr: bool = False) -> Console:
    """A console that does not mangle piped output.

    Rich assumes 80 columns when it is not writing to a terminal, which folds
    table cells in half the moment the output is piped into `less` or captured
    in CI. Tables here are content-sized rather than expanded, so giving a
    non-terminal a generous width changes nothing except that rows stop
    wrapping.
    """
    interactive = (sys.stderr if stderr else sys.stdout).isatty()
    return Console(
        stderr=stderr,
        width=None if interactive else 200,
        soft_wrap=False,
        # Every string that reaches this console may contain text an upstream
        # server chose — an error message, a tool description, a registry entry.
        # With markup on, Rich would read `[code -32603]` as a style tag and
        # silently swallow it, and a hostile description could restyle the
        # surrounding output. Styling is applied by constructing `Text` objects
        # instead, which is explicit and cannot be injected into.
        markup=False,
        highlight=False,
    )


@dataclass(slots=True)
class Output:
    """The CLI's writer. One instance per invocation, held on the Typer context."""

    json_mode: bool = False
    quiet: bool = False
    stdout: Console = field(default_factory=_console)
    stderr: Console = field(default_factory=lambda: _console(stderr=True))

    # ------------------------------------------------------------------- data

    def emit(self, payload: Any, renderer: Callable[[], RenderableType]) -> None:
        """Write one command's result.

        Args:
            payload: The machine-readable form, printed verbatim under `--json`.
            renderer: Builds the human form. Called only when it will be shown,
                so table construction costs nothing in JSON mode.
        """
        if self.json_mode:
            self.json(payload)
            return
        self.stdout.print(renderer())

    def json(self, payload: Any) -> None:
        """Write a JSON document to stdout.

        `default=str` is a deliberate safety net: payloads carry datetimes and
        enums, and a serialisation error at the last step would lose a result the
        hub has already produced.
        """
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        sys.stdout.flush()

    def print(self, renderable: RenderableType) -> None:
        """Write a renderable to stdout, unless in JSON mode."""
        if not self.json_mode:
            self.stdout.print(renderable)

    # ------------------------------------------------------------ diagnostics

    def note(self, message: str) -> None:
        """Progress detail. Suppressed when quiet or emitting JSON."""
        if not self.quiet and not self.json_mode:
            self.stderr.print(Text(message, style="dim"))

    def success(self, message: str) -> None:
        """A completed action."""
        if not self.quiet and not self.json_mode:
            self.stderr.print(_prefixed("✓", "green", message))

    def warn(self, message: str) -> None:
        """Something an operator should know but that does not stop the command."""
        if not self.json_mode:
            self.stderr.print(_prefixed("!", "yellow", message))

    def fail(self, message: str) -> None:
        """A failure. Always shown, in every mode, on stderr."""
        self.stderr.print(_prefixed("✗", "bold red", message))

    def error_detail(self, code: str, details: Mapping[str, Any]) -> None:
        """The machine-readable part of a `HubError`, for a person to quote."""
        if self.json_mode or self.quiet:
            return
        for key, value in (("code", code), *details.items()):
            line = Text("  ")
            line.append(f"{key}: ", style="dim")
            line.append(str(value))
            self.stderr.print(line)

    # ----------------------------------------------------------- confirmation

    def confirm(self, question: str, *, assume_yes: bool = False, default: bool = False) -> bool:
        """Ask before doing something irreversible (arch §58).

        Returns `False` rather than prompting when there is no terminal to prompt
        on. A CI job that reaches a confirmation without `--yes` has a bug in its
        pipeline, and stopping is the answer that cannot destroy anything.
        """
        if assume_yes:
            return True
        if self.json_mode or not sys.stdin.isatty():
            self.fail(
                "Refusing to continue: confirmation is required and this is not an interactive "
                "terminal. Pass --yes to confirm explicitly."
            )
            return False

        from rich.prompt import Confirm

        return bool(Confirm.ask(question, default=default, console=self.stderr))


def _prefixed(marker: str, style: str, message: str) -> Text:
    """A styled marker followed by literal message text."""
    line = Text(marker + " ", style=style)
    line.append(message, style="")
    return line


# --------------------------------------------------------------------------- tables


def _table(*columns: str, title: str | None = None) -> Table:
    """A table styled the same way everywhere."""
    table = Table(title=title, box=None, pad_edge=False, header_style="bold", title_justify="left")
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def _cell(value: Any) -> Text:
    """A table cell holding literal text.

    Cells carry upstream-supplied strings, so they are built as `Text` rather
    than left as `str`: a bare string would be re-parsed for markup even on a
    console with markup disabled by a caller that forgot.
    """
    return Text("" if value is None else str(value))


def _styled(value: str, styles: Mapping[str, str]) -> Text:
    """Colour a status-like value by lookup, leaving unknown values plain."""
    return Text(value, style=styles.get(value, ""))


def _shorten(value: str | None, limit: int = 24) -> str:
    """Truncate long version identifiers so a table stays one row per item."""
    if not value:
        return "-"
    return value if len(value) <= limit else value[: limit - 1] + "…"


def integrations_table(rows: Sequence[Mapping[str, Any]], *, probed: bool = True) -> RenderableType:
    """`mcp-hub list` (arch §14)."""
    if not rows:
        return Text(
            "No integrations configured. Add a manifest under config/manifests/ "
            "or run `mcp-hub registry search <term>`.",
            style="dim",
        )

    table = _table("ID", "SOURCE", "TRUST", "STATE", "VERSION", "HEALTH", "TOOLS", "UPDATES")
    for row in rows:
        if not row["enabled"]:
            state = Text("disabled", style="dim")
        elif not row["installed"]:
            state = Text("not installed", style="yellow")
        else:
            state = Text("enabled", style="green")
        table.add_row(
            _cell(row["id"]),
            _cell(row["source_type"]),
            _styled(str(row["trust"]), _TRUST_STYLES),
            state,
            _cell(_shorten(row.get("version"))),
            # Without a probe the only honest answer is that nothing was measured.
            # Showing the baseline guess here would read as an observation.
            _styled(str(row["health"]), _HEALTH_STYLES) if probed else Text("-", style="dim"),
            _cell(row["tools"] if probed else "-"),
            _cell(row["update_policy"]),
        )
    footer = Text(
        f"\n{len(rows)} integration(s)."
        + ("" if probed else " Health and tool counts omitted: upstreams were not contacted (--no-probe)."),
        style="dim",
    )
    return Group(table, footer)


def tools_table(rows: Sequence[Mapping[str, Any]], *, exposure_mode: str = "") -> RenderableType:
    """`mcp-hub tools [integration]` (arch §51)."""
    if not rows:
        return Text("No tools registered. Upstreams may be unreachable — try `mcp-hub health`.", style="dim")

    table = _table("TOOL", "RISK", "CONFIRM", "STATE", "DESCRIPTION")
    for row in rows:
        if not row["enabled"]:
            state = Text("disabled", style="dim")
        elif not row["allowed"]:
            state = Text("policy-denied", style="red")
        else:
            state = Text("exposed", style="green")
        table.add_row(
            _cell(row["qualified_name"]),
            _styled(str(row["risk"]), _RISK_STYLES),
            _cell("yes" if row["requires_confirmation"] else ""),
            state,
            _cell(row.get("description") or ""),
        )
    footer = Text(
        f"\n{len(rows)} tool(s)." + (f" Exposure mode: {exposure_mode}." if exposure_mode else ""),
        style="dim",
    )
    return Group(table, footer)


def health_table(statuses: Mapping[str, Mapping[str, Any]]) -> RenderableType:
    """`mcp-hub health` (arch §25)."""
    if not statuses:
        return Text("No integrations to probe.", style="dim")

    table = _table("INTEGRATION", "STATUS", "LATENCY", "TOOLS", "DETAIL")
    for integration_id, report in statuses.items():
        latency = report.get("latency_ms")
        table.add_row(
            _cell(integration_id),
            _styled(str(report["status"]), _HEALTH_STYLES),
            _cell(f"{latency:.0f} ms" if isinstance(latency, int | float) else "-"),
            _cell(report.get("tools", 0)),
            _cell(report.get("detail") or ""),
        )
    healthy = sum(1 for report in statuses.values() if report["status"] == "HEALTHY")
    footer = Text(f"\n{healthy}/{len(statuses)} healthy.", style="dim")
    return Group(table, footer)


def doctor_report(checks: Sequence[Mapping[str, Any]]) -> RenderableType:
    """`mcp-hub doctor` — arch §36's exact `[OK] Subject` shape."""
    lines: list[Text] = []
    width = max((len(str(check["subject"])) for check in checks), default=0)
    for check in checks:
        status = str(check["status"])
        line = Text()
        line.append(f"[{status}]".ljust(7), style=_CHECK_STYLES.get(status, ""))
        line.append(str(check["subject"]).ljust(width + 2))
        line.append(str(check["detail"]), style="dim")  # append() is literal, not markup
        lines.append(line)

    failures = sum(1 for check in checks if check["status"] == "FAIL")
    warnings = sum(1 for check in checks if check["status"] == "WARN")
    if failures:
        summary = Text(f"\n{failures} check(s) failed, {warnings} warning(s).", style="bold red")
    elif warnings:
        summary = Text(f"\nAll required checks passed, {warnings} warning(s).", style="yellow")
    else:
        summary = Text("\nAll checks passed.", style="green")
    return Group(*lines, summary)


def outcomes_table(outcomes: Sequence[Mapping[str, Any]]) -> RenderableType:
    """The result of an update run (arch §16)."""
    if not outcomes:
        return Text("Nothing was updated.", style="dim")

    table = _table("INTEGRATION", "RESULT", "FROM", "TO", "TOOLS", "DETAIL")
    for outcome in outcomes:
        if outcome["succeeded"]:
            result = Text("updated", style="green")
        elif outcome["rolled_back"]:
            result = Text("failed — rolled back", style="yellow")
        else:
            result = Text(f"failed at {outcome['stage']}", style="bold red")
        added, removed = len(outcome["tools_added"]), len(outcome["tools_removed"])
        churn = ", ".join(part for part in (f"+{added}" if added else "", f"-{removed}" if removed else "") if part)
        table.add_row(
            _cell(outcome["integration"]),
            result,
            _cell(_shorten(outcome.get("from"))),
            _cell(_shorten(outcome.get("to"))),
            _cell(churn or "-"),
            _cell(outcome.get("detail") or ""),
        )

    succeeded = sum(1 for outcome in outcomes if outcome["succeeded"])
    style = "green" if succeeded == len(outcomes) else "bold red"
    footer = Text(f"\n{succeeded}/{len(outcomes)} succeeded.", style=style)

    # Tool changes are the part an agent notices, so they are spelled out rather
    # than left as a count (arch §15).
    detail: list[RenderableType] = [table, footer]
    for outcome in outcomes:
        if outcome["tools_removed"]:
            detail.append(
                Text(
                    f"\n{outcome['integration']}: removed {', '.join(outcome['tools_removed'])}",
                    style="yellow",
                )
            )
        if outcome["tools_added"]:
            detail.append(Text(f"{outcome['integration']}: added {', '.join(outcome['tools_added'])}", style="dim"))
    return Group(*detail)


def rollback_points_table(points: Sequence[Mapping[str, Any]]) -> RenderableType:
    """`mcp-hub rollback <name> --list` (arch §19)."""
    if not points:
        return Text(
            "No rollback points. One is created before every update, so there has been none yet.",
            style="dim",
        )
    table = _table("VERSION", "CREATED", "REASON", "TOOLS", "BACKUP")
    for point in points:
        table.add_row(
            _cell(_shorten(str(point.get("version")))),
            _cell(str(point.get("created_at") or "-")[:19].replace("T", " ")),
            _cell(point.get("reason") or "-"),
            _cell(point.get("tools", "-")),
            Text(str(point.get("id") or "-"), style="dim"),
        )
    footer = Text(
        "\nRestore the most recent with `mcp-hub rollback <name>`, or a specific one with `--version <VERSION>`.",
        style="dim",
    )
    return Group(table, footer)


def audit_table(records: Sequence[Mapping[str, Any]]) -> RenderableType:
    """`mcp-hub logs` (arch §24)."""
    if not records:
        return Text("No audit records match.", style="dim")

    table = _table("TIME", "ACTION", "STATUS", "USER", "INTEGRATION", "TOOL", "DETAIL")
    for record in records:
        status = str(record["status"])
        table.add_row(
            _cell(str(record["timestamp"])[:19].replace("T", " ")),
            _cell(record["action"]),
            Text(status, style="green" if status == "success" else "bold red"),
            _cell(record.get("user_id") or "-"),
            _cell(record.get("integration") or "-"),
            _cell(record.get("tool") or "-"),
            _cell(record.get("message") or record.get("error_code") or ""),
        )
    return Group(table, Text(f"\n{len(records)} record(s), newest first.", style="dim"))


def registry_table(servers: Sequence[Mapping[str, Any]]) -> RenderableType:
    """`mcp-hub registry search <query>` (arch §7)."""
    if not servers:
        return Text("No servers matched.", style="dim")

    table = _table("NAME", "VERSION", "INSTALLABLE", "DESCRIPTION")
    for server in servers:
        name = Text(str(server["name"]))
        if server.get("deprecated"):
            name.append("  (deprecated)", style="yellow")
        table.add_row(
            name,
            _cell(_shorten(server.get("version"), 16)),
            _cell("yes") if server.get("installable") else Text("no", style="dim"),
            _cell(str(server.get("description") or "")[:90]),
        )
    footer = Text(
        f"\n{len(servers)} result(s). Inspect one with `mcp-hub registry inspect <name>` before installing.",
        style="dim",
    )
    return Group(table, footer)


def disclosure_panel(rendered: str, *, requires_confirmation: bool) -> RenderableType:
    """The pre-install disclosure (arch §54).

    Framed rather than printed flat because it is the one screen an operator must
    actually read before third-party code runs on the host.
    """
    from rich.panel import Panel

    border = "yellow" if requires_confirmation else "cyan"
    subtitle = (
        "Community server — running this executes third-party code on this host." if requires_confirmation else None
    )
    return Panel(rendered, title="Install disclosure", border_style=border, subtitle=subtitle)


def status_report(payload: Mapping[str, Any]) -> RenderableType:
    """`mcp-hub status` — one screen describing what the hub is doing."""
    integrations = payload["integrations"]
    rows = [
        ("Version", str(payload["version"])),
        ("Environment", str(payload["environment"])),
        ("MCP endpoint", str(payload["mcp_endpoint"])),
        ("Exposure mode", str(payload["exposure_mode"])),
        (
            "Integrations",
            f"{integrations['healthy']} healthy / {integrations['enabled']} enabled / "
            f"{integrations['total']} configured",
        ),
        ("Tools", str(payload["tools"])),
        ("Upstream sessions", str(len(payload.get("sessions", ())))),
        ("Secret providers", ", ".join(payload.get("secret_providers", ())) or "none"),
        ("Coordination", str(payload["coordination"])),
    ]
    table = _table("", "")
    table.show_header = False
    for label, value in rows:
        table.add_row(Text(label, style="dim"), _cell(value))
    return table
