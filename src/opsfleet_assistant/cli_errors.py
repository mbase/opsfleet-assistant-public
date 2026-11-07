"""JSON usage errors before Typer command dispatch, without opening dependencies."""
from collections.abc import Sequence
import json
import sys

import click
from typer.core import TyperGroup

from .navigation import NavigationResult


class JsonUsageGroup(TyperGroup):
    def main(self, args: Sequence[str] | None = None, prog_name=None, complete_var=None,
             standalone_mode=True, windows_expand_args=True, **extra):
        arguments = list(sys.argv[1:] if args is None else args)
        operation = arguments[0] if arguments and arguments[0] in {'reports', 'report', 'conversations', 'ask', 'chat'} else 'navigation'
        options = arguments[:arguments.index('--')] if '--' in arguments else arguments
        json_command = any(option == '--json' or option.startswith('--json=') for option in options)
        if not json_command:
            return super().main(arguments, prog_name, complete_var, standalone_mode,
                                windows_expand_args, **extra)
        try:
            result = super().main(arguments, prog_name, complete_var, False,
                                  windows_expand_args, **extra)
        except click.UsageError:
            # Error strings can echo supplied values. The public error contains
            # only a fixed category/help hint, never a parameter or capability.
            click.echo(json.dumps(NavigationResult(operation, status='invalid_command',
                message='Invalid command arguments. See command --help.').payload()))
            result = 2
        if standalone_mode:
            raise SystemExit(result if isinstance(result, int) else 0)
        return result
