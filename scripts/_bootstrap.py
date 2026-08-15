"""Shared plumbing for the `scripts/*.py` wrappers (arch §9, §57, §58).

Arch §57 asks for `scripts/update.py` and says, in the same breath, "do not
duplicate update logic". Both halves are honoured here: every wrapper in this
directory is a name and one call to `delegate()`, which runs the matching
`mcp-hub` subcommand in this process. There is no argument parsing, no
confirmation prompt and no service call anywhere in `scripts/` — a flag added
to the CLI works from the wrappers the day it lands, and a wrapper cannot
drift into a second, differently-behaved control plane.

Run them from anywhere:

    python scripts/update.py --all --dry-run

The repository root is put on `sys.path` first, so this works from a checkout
whose dependencies are installed but whose package is not (`pip install -r`
rather than `pip install -e .`). When the package *is* installed, `mcp-hub
update --all --dry-run` is the same command by a shorter name.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

_ROOT = Path(__file__).resolve().parent.parent


def delegate(command: str) -> NoReturn:
    """Run `mcp-hub <command>` with this process's arguments, then exit.

    Args:
        command: The subcommand to run, e.g. `"update"`. Wrappers pass a
            literal; nothing derives it from the filename, so a renamed script
            fails loudly at review time rather than quietly running the wrong
            operation.

    Returns:
        Never. Click exits the process with the command's status code, which is
        the CLI's documented contract: 0 success, 1 the operation ran and
        failed, 2 bad usage, 130 interrupted or declined.

    Help and errors are rendered as `mcp-hub <command>` rather than as the
    script path, because that is what this invocation *is* — the wrapper is a
    second spelling of one command, and an operator reading `--help` should
    come away knowing the canonical one.
    """
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from typer.core import TyperOption
    from typer.main import get_command

    from app.cli.commands import app

    cli = get_command(app)
    # Which options belong to the root callback is read off the command object
    # rather than listed here, so a new global flag on the CLI cannot leave the
    # wrappers behind. `True` means the option consumes the next argument.
    valued = {
        option: not (parameter.is_flag or parameter.count)
        for parameter in cli.params
        if isinstance(parameter, TyperOption)
        for option in (*parameter.opts, *parameter.secondary_opts)
    }
    root, rest = _partition(sys.argv[1:], valued)
    cli(args=[*root, command, *rest], prog_name="mcp-hub", standalone_mode=True)
    raise SystemExit(0)  # click.main() exits; this only satisfies `NoReturn`.


def _partition(argv: Sequence[str], valued: Mapping[str, bool]) -> tuple[list[str], list[str]]:
    """Split arguments into the ones the root group owns and everything else.

    `--json`, `--config-dir` and friends belong to the root callback, so click
    only accepts them *before* the subcommand: `mcp-hub --json update --all`.
    A wrapper's user has no subcommand to put them before — they write
    `python scripts/update.py --all --json` — so those options are lifted out
    and re-inserted where click expects them.

    Args:
        argv: Arguments as given, without the program name.
        valued: Root option spellings mapped to whether each takes a value.

    Returns:
        The root options, and everything else in its original order. Anything
        unrecognised is left alone for the subcommand to accept or reject, so a
        typo still produces click's own error rather than one invented here.
    """
    root: list[str] = []
    rest: list[str] = []
    pending = list(argv)
    while pending:
        argument = pending.pop(0)
        if argument == "--":  # everything after this is positional, by definition
            rest.append(argument)
            rest.extend(pending)
            break
        name, separator, _ = argument.partition("=")
        if name in valued:
            root.append(argument)
            if valued[name] and not separator and pending:
                root.append(pending.pop(0))
        elif _is_flag_cluster(name, valued):
            root.append(argument)
        else:
            rest.append(argument)
    return root, rest


def _is_flag_cluster(argument: str, valued: Mapping[str, bool]) -> bool:
    """True for `-vv` or `-qv`: several value-less short flags in one token."""
    if len(argument) < 3 or not argument.startswith("-") or argument.startswith("--"):
        return False
    return all(valued.get(f"-{character}") is False for character in argument[1:])
